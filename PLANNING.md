# OLAP App Migration Plan: CSV -> Databricks

## 1) Goals
- Run the app in Databricks (primary runtime).
- Read model-driven data from Databricks tables/views instead of CSV files.
- Keep the app fully testable in local dev (this workspace).
- Preserve the current contract: frontend sends generic query intent, backend returns a DataFrame.

---

## 2) Target Architecture

### Keep
- `models/*.yaml` as semantic model definitions.
- Frontend (`app.py`) model dropdown + grid behavior.
- Generic request object (`OlapQueryRequest`).

### Replace/Extend
- Current `CsvBackend` becomes one backend implementation.
- Add `DatabricksBackend` implementing the same interface (`execute()`, `flat_table()`).

### Backend Selection
- Introduce `DATA_BACKEND` config:
	- `csv` (local/default for tests)
	- `databricks` (runtime in Databricks)

---

## 3) Model YAML Changes (Minimal + Backward Compatible)

Add optional Databricks metadata to each table source.

Example pattern:
- `source.csv`: local path for local mode
- `source.databricks`: `catalog.schema.table` (or view)

For each fact/dimension in YAML:
- Keep existing CSV `source` (or move to `source.csv`)
- Add Databricks table identifier

This allows one model to work in both local and Databricks modes.

---

## 4) Databricks Data Access Strategy

## Option A (recommended): Databricks SQL Warehouse + Connector
- Use `databricks-sql-connector`.
- Build SQL from `OlapQueryRequest` + model metadata.
- Execute on SQL Warehouse, return pandas DataFrame.

## Option B: Spark session (inside Databricks notebooks/jobs)
- Use `spark.table()` and DataFrame ops.
- Convert result to pandas only at output boundary.

## Decision
- Use Option A for app runtime consistency and easier local parity.

---

## 5) Query Compilation Plan

Implement a query compiler in backend layer:

1. Resolve model and requested fields.
2. Build FROM fact table.
3. Add JOIN clauses from YAML relationships (`fact_key` -> `dim_key`).
4. Apply filters.
5. Apply grouping/aggregation.
6. Optional pivot behavior:
	 - Prefer frontend pivot (AG Grid) by returning flat denormalized data for interactivity.
	 - Keep backend aggregate support for large datasets or export workflows.

Output remains pandas DataFrame.

---

## 6) Runtime in Databricks

## Packaging
- Add dependencies to `requirements.txt`:
	- `databricks-sql-connector`
	- `PyYAML` (if not already pinned)

## Configuration via env vars
- `DATA_BACKEND=databricks`
- `DATABRICKS_SERVER_HOSTNAME`
- `DATABRICKS_HTTP_PATH`
- `DATABRICKS_TOKEN` (or OAuth/service principal flow)
- `DATABRICKS_CATALOG` / `DATABRICKS_SCHEMA` (optional defaults)

## Secrets
- In Databricks runtime, load credentials from secret scopes / app secrets.
- Do not hardcode credentials in YAML.

---

## 7) Local Testability Strategy

### Unit Tests (local, fast)
- Keep `CsvBackend` as baseline backend.
- Add tests for:
	- model parsing
	- join correctness
	- filter/group/metric behavior
	- backend interface contract

### Databricks Backend Tests (local without Databricks)
- Mock SQL connector cursor responses.
- Validate generated SQL + parameter binding.
- Validate DataFrame schema mapping.

### Optional Integration Tests
- Enabled only when Databricks env vars are present.
- Run a small smoke query against a test SQL Warehouse.

---

## 8) Performance & Scale Controls
- Add row-limit guardrails for default flat-table fetches.
- Add server-side pagination or sampling mode for very large fact tables.
- Push filters to SQL whenever possible.
- Cache dimension tables/metadata where safe.

---

## 9) Security & Governance
- Use Unity Catalog table identifiers in YAML for Databricks sources.
- Enforce least-privilege warehouse permissions.
- Audit query usage via Databricks query history.
- Optionally restrict allowed columns/metrics at model layer.

---

## 10) Incremental Delivery Plan

## Phase 1: Infrastructure
- Add backend config switch.
- Add Databricks connector dependency.
- Add env/secret config loader.

## Phase 2: Backend
- Implement `DatabricksBackend` with `flat_table()` parity first.
- Then add grouped query support in `execute()`.

## Phase 3: Model Enhancements
- Extend YAML with Databricks table identifiers.
- Keep CSV sources for local mode.

## Phase 4: Testing
- Add unit tests + SQL compiler tests.
- Add optional integration smoke tests.

## Phase 5: Databricks Deployment
- Configure runtime env/secrets.
- Validate both models (`Sales`, `Purchasing`) in app dropdown.

---

## 11) Definition of Done
- App runs in Databricks with `DATA_BACKEND=databricks`.
- Same app runs locally with `DATA_BACKEND=csv`.
- No frontend code changes needed when switching backends.
- Both existing models work in both modes.
- Automated tests pass locally.
