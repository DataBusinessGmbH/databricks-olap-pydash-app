"""
app.py  –  Frontend layer
-------------------------
This file contains ONLY UI concerns:
  * Build AG Grid column definitions from the model (no hard-coded field names)
  * Ask the database layer for a flat table via OlapQueryRequest
  * Render the Dash layout

No knowledge of CSV paths, join keys, or metric names lives here.
To point at a different backend (Databricks, Snowflake, …) swap the
DatabricksBackend instantiation for another OlapDatabase implementation.
"""

from pathlib import Path
import os
import json
import re
import importlib
import functools
from typing import Any
from flask import jsonify, request as flask_request
from databricks import sql

import dash_ag_grid as dag
import pandas as pd
from dash import Dash, Input, Output, State, dcc, html, no_update
import dash

from src.model import MetricViewDef, MvField, metric_view_def_to_dict, metric_view_def_from_dict
from src.db import (
    LOGGER,
    OlapDatabase,
    OlapQueryRequest,
    create_databricks_backend,
)
from src.query_planner import (
    execute_olap_request as planner_execute_olap_request,
    fetch_filter_values as planner_fetch_filter_values,
)
import logging

# ---------------------------------------------------------------------------
# Bootstrap: load model, connect database layer
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
DEFAULT_MAX_ROWS = 1000
STARTUP_WARNINGS: list[str] = []


def ensure_logging_visible() -> None:
    """Ensure INFO logs are visible even when Flask preconfigures root logging."""
    level_name = os.getenv("APP_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers:
        handler.setLevel(level)

ensure_logging_visible()


def load_env_on_startup() -> None:
    """
    Load environment variables from .env automatically.

    - If .env exists, load it without overriding already-exported environment vars.
    - If .env does not exist, default backend mode to sql.
    """
    env_path = BASE_DIR / ".env"

    if env_path.exists():
        LOGGER.info("Loading environment variables from %s", env_path)
        try:
            dotenv = __import__("dotenv")
            dotenv.load_dotenv(dotenv_path=env_path, override=False)

            # Logging loaded env vars for visibility (masking sensitive values)
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    name, value = line.split("=", 1)
                    name = name.strip()
                    # Strip possible export keyword and whitespace from name, and whitespace from value
                    if name.lower().startswith("export "):
                        name = name[7:].strip()
                    value = value.strip()
                    if name and value:
                        display_value = (
                            "****" if "TOKEN" in name.upper() or "KEY" in name.upper() else value
                        )
                        LOGGER.info("Loaded env var: %s=%s", name, os.getenv(name))

        except Exception:
            # If python-dotenv is unavailable, continue with existing process env.
            pass

    # when running locally, app.yaml env entries are not injected by the
    # platform runtime, so load them explicitly as fallback defaults.
    app_yaml_path = BASE_DIR / "app.yaml"
    if app_yaml_path.exists():
        LOGGER.info("Loading environment variables from %s", app_yaml_path)
        try:
            yaml_mod = importlib.import_module("yaml")
            app_cfg = yaml_mod.safe_load(app_yaml_path.read_text(encoding="utf-8")) or {}
            env_entries = app_cfg.get("env", []) if isinstance(app_cfg, dict) else []

            loaded_names: list[str] = []
            for entry in env_entries if isinstance(env_entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "").strip()
                value = entry.get("value")
                if not name or value is None:
                    continue

                if name not in os.environ:
                    loaded_names.append(name)
                os.environ.setdefault(name, str(value))
                LOGGER.info("Loaded env var from app.yaml: %s=%s", name, value)

        except Exception:
            LOGGER.warning("Failed to load env entries from app.yaml", exc_info=True)
    
load_env_on_startup()


def _safe_model_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "")).strip("._-")


def metric_view_model_id(catalog: str, schema: str, metric_view_name: str) -> str:
    safe_catalog = _safe_model_token(catalog) or "catalog"
    safe_stem = _safe_model_token(metric_view_name) or "metricview"
    safe_schema = _safe_model_token(schema) or "schema"
    return f"metricview_{safe_catalog}_{safe_schema}_{safe_stem}"


def register_metric_views(
    catalog: str,
    schema: str,
    registry: dict[str, MetricViewDef],
) -> dict[str, str]:
    """Discover, parse, and register all metric views for catalog/schema into a local registry."""
    model_ids: dict[str, str] = {}  # model_id → metric_view_name

    mv_defs = load_metric_views(catalog, schema)
    for mv_name, mv_def in mv_defs.items():
        model_id = mv_def.model_id

        if model_id in model_ids or model_id in registry:
            suffix = 1
            unique_id = f"{model_id}_{suffix}"
            while unique_id in model_ids or unique_id in registry:
                suffix += 1
                unique_id = f"{model_id}_{suffix}"
            STARTUP_WARNINGS.append(
                f"Metric view model id conflict for {model_id}; registered as {unique_id}."
            )
            model_id = unique_id
            mv_def = MetricViewDef(
                model_id=model_id,
                catalog=mv_def.catalog,
                schema=mv_def.schema,
                metric_view_name=mv_def.metric_view_name,
                display_name=mv_def.display_name,
                fields=mv_def.fields,
            )

        registry[model_id] = mv_def
        model_ids[model_id] = mv_name

    if not model_ids:
        LOGGER.info("No metric views registered in %s.%s", catalog, schema)
    LOGGER.info("Registered metric views for %s.%s: %s", catalog, schema, model_ids)
    return model_ids


def _quoted_ident(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _extract_first_non_null_str(df: pd.DataFrame) -> list[str]:
    values: list[str] = []
    if df.empty:
        return values

    for col in df.columns:
        for raw in df[col].tolist():
            if pd.isna(raw):
                continue
            text = str(raw).strip()
            if text:
                values.append(text)
        if values:
            return values
    return values


def _parse_csv_env(var_name: str) -> list[str]:
    raw = (os.getenv(var_name) or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def _exclude_information_schema(names: list[str]) -> list[str]:
    return [n for n in names if str(n).strip().lower() != "information_schema"]


def _exclude_non_reporting_schemas(names: list[str]) -> list[str]:
    excluded = {"information_schema", "default"}
    return [n for n in names if str(n).strip().lower() not in excluded]


def _normalize_report_filters(raw_filters) -> dict[str, dict[str, list] | list]:
    if not isinstance(raw_filters, dict):
        return {}
    parsed: dict[str, dict[str, list] | list] = {}
    for field, spec in raw_filters.items():
        if isinstance(spec, dict):
            includes = spec.get("in", [])
            excludes = spec.get("not_in", [])
            includes = includes if isinstance(includes, list) else [includes]
            excludes = excludes if isinstance(excludes, list) else [excludes]
            filter_spec: dict[str, list] = {}
            in_clean = [v for v in includes if v not in (None, "")]
            not_in_clean = [v for v in excludes if v not in (None, "")]
            if in_clean:
                filter_spec["in"] = in_clean
            if not_in_clean:
                filter_spec["not_in"] = not_in_clean
            if filter_spec:
                parsed[str(field)] = filter_spec
            continue

        values = spec if isinstance(spec, list) else [spec]
        values = [v for v in values if v not in (None, "")]
        if values:
            parsed[str(field)] = {"in": values}
    return parsed


def _normalize_report_dimension_fields(raw_dimension_fields) -> dict[str, list[str]]:
    if not isinstance(raw_dimension_fields, dict):
        return {}

    parsed: dict[str, list[str]] = {}
    for dim_name, fields in raw_dimension_fields.items():
        if fields is None:
            continue
        values = fields if isinstance(fields, list) else [fields]
        cleaned = [str(v).strip() for v in values if str(v).strip()]
        if cleaned:
            parsed[str(dim_name).strip()] = cleaned
    return parsed

def _parse_keyfigure_expr(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None

    # Convention: SUM(MEASURE(`Sales net`)) or MEASURE(`Sales net`).
    # The outer aggregation function (if present) is intentionally ignored,
    # because aggregation semantics come from the metric view.
    match_nested = re.fullmatch(
        r"(?i)\s*[A-Za-z_][A-Za-z0-9_]*\s*\(\s*MEASURE\s*\(\s*`([^`]+)`\s*\)\s*\)\s*",
        text,
    )
    if match_nested:
        measure_ref = str(match_nested.group(1)).strip()
        return measure_ref or None

    match = re.fullmatch(
        r"(?i)\s*MEASURE\s*\(\s*`([^`]+)`\s*\)\s*",
        text,
    )
    if not match:
        return None

    measure_ref = str(match.group(1)).strip()
    return measure_ref or None


def load_report_definitions() -> dict[str, dict[str, Any]]:
    reports: dict[str, dict[str, Any]] = {}
    if not REPORTS_DIR.exists():
        return reports

    for path in sorted(REPORTS_DIR.glob("*.y*ml")):
        report_id = path.stem
        try:
            raw = _yaml_safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            STARTUP_WARNINGS.append(f"Failed reading report YAML {path.name}; skipping.")
            continue

        if not isinstance(raw, dict):
            STARTUP_WARNINGS.append(f"Report file {path.name} must contain a YAML object.")
            continue

        name = str(raw.get("name") or raw.get("title") or report_id)
        dimensions_raw = raw.get("dimensions", raw.get("rows", []))
        dimensions: list[str] = []
        dimension_fields_raw = raw.get("dimension_fields", {})

        if isinstance(dimensions_raw, list):
            for entry in dimensions_raw:
                if isinstance(entry, str):
                    dimensions.append(entry)
                    continue
                if isinstance(entry, dict):
                    dim_name = str(entry.get("name") or "").strip()
                    if dim_name:
                        dimensions.append(dim_name)
                        if "fields" in entry and dim_name not in dimension_fields_raw:
                            dimension_fields_raw[dim_name] = entry.get("fields")

        keyfigures = raw.get("keyfigures", raw.get("metrics", []))
        report_def: dict[str, Any] = {
            "id": report_id,
            "name": name,
            "model_id": raw.get("model_id"),
            "catalog": raw.get("catalog"),
            "schema": raw.get("schema"),
            "metric_view": raw.get("metric_view"),
            "dimensions": dimensions,
            "dimension_fields": _normalize_report_dimension_fields(dimension_fields_raw),
            "keyfigures": keyfigures if isinstance(keyfigures, list) else [],
            "filters": _normalize_report_filters(raw.get("filters", {})),
        }
        reports[report_id] = report_def

    LOGGER.info("Loaded %s report definition(s) from %s", len(reports), REPORTS_DIR)
    return reports


def resolve_report_model_id(report: dict[str, Any], catalog: str, schema: str) -> str | None:
    model_id = str(report.get("model_id") or "").strip()
    if model_id:
        return model_id

    report_catalog = str(report.get("catalog") or "").strip()
    if report_catalog and report_catalog != catalog:
        return None

    metric_view = str(report.get("metric_view") or "").strip()
    if not metric_view:
        return None

    report_schema = str(report.get("schema") or schema).strip()
    if not report_schema:
        return None

    return metric_view_model_id(catalog, report_schema, metric_view)


def _build_access_matrix() -> tuple[pd.DataFrame, dict[str, MetricViewDef]]:
    rows: list[dict[str, str]] = []
    registry: dict[str, MetricViewDef] = {}

    for catalog in list_catalog_names():
        for schema in list_schema_names(catalog):
            model_map = register_metric_views(catalog, schema, registry)
            for model_id, metric_view_name in model_map.items():
                rows.append(
                    {
                        "catalog": catalog,
                        "schema": schema,
                        "model_id": model_id,
                        "metric_view": metric_view_name,
                        "report_id": "__no_report__",
                        "report_name": "No Report",
                    }
                )

                for report_id, report in REPORT_DEFS.items():
                    resolved_model = resolve_report_model_id(report, catalog, schema)
                    if resolved_model == model_id:
                        rows.append(
                            {
                                "catalog": catalog,
                                "schema": schema,
                                "model_id": model_id,
                                "metric_view": metric_view_name,
                                "report_id": report_id,
                                "report_name": str(report.get("name") or report_id),
                            }
                        )

    access_df = pd.DataFrame(rows, columns=["catalog", "schema", "model_id", "metric_view", "report_id", "report_name"])
    return access_df, registry


def _unique_options(df: pd.DataFrame, value_col: str) -> list[dict[str, str]]:
    if df.empty:
        return []
    deduped = (
        df[[value_col]]
        .dropna(subset=[value_col])
        .drop_duplicates(subset=[value_col], keep="first")
        .sort_values(by=[value_col])
    )
    return [{"label": str(row[value_col]), "value": str(row[value_col])} for _, row in deduped.iterrows()]


def get_catalog_dropdown_options_from_matrix(access_matrix_df: pd.DataFrame) -> list[dict[str, str]]:
    if access_matrix_df.empty:
        return []
    return _unique_options(access_matrix_df, "catalog")


def get_schema_dropdown_options_from_matrix(access_matrix_df: pd.DataFrame, catalog: str | None) -> list[dict[str, str]]:
    if not catalog or access_matrix_df.empty:
        return []
    scoped = access_matrix_df[access_matrix_df["catalog"] == str(catalog)]
    return _unique_options(scoped, "schema")


def get_all_schema_dropdown_options_from_matrix(access_matrix_df: pd.DataFrame) -> list[dict[str, str]]:
    if access_matrix_df.empty:
        return []
    return _unique_options(access_matrix_df, "schema")


def get_model_dropdown_options_from_matrix(
    access_matrix_df: pd.DataFrame,
    catalog: str | None,
    schema: str | None,
) -> list[dict[str, str]]:
    if not catalog or not schema or access_matrix_df.empty:
        return []
    scoped = access_matrix_df[
        (access_matrix_df["catalog"] == str(catalog))
        & (access_matrix_df["schema"] == str(schema))
    ]
    if scoped.empty:
        return []

    deduped = (
        scoped[["model_id", "metric_view"]]
        .dropna(subset=["model_id"])
        .drop_duplicates(subset=["model_id"], keep="first")
        .sort_values(by=["metric_view", "model_id"])
    )
    return [
        {
            "label": str(row["metric_view"]) if pd.notna(row["metric_view"]) and str(row["metric_view"]).strip() else str(row["model_id"]),
            "value": str(row["model_id"]),
        }
        for _, row in deduped.iterrows()
    ]


def get_all_model_dropdown_options_from_matrix(access_matrix_df: pd.DataFrame) -> list[dict[str, str]]:
    if access_matrix_df.empty:
        return []
    deduped = (
        access_matrix_df[["model_id", "metric_view"]]
        .dropna(subset=["model_id"])
        .drop_duplicates(subset=["model_id"], keep="first")
        .sort_values(by=["metric_view", "model_id"])
    )
    return [
        {
            "label": str(row["metric_view"]) if pd.notna(row["metric_view"]) and str(row["metric_view"]).strip() else str(row["model_id"]),
            "value": str(row["model_id"]),
        }
        for _, row in deduped.iterrows()
    ]


def get_report_dropdown_options(
    access_matrix_df: pd.DataFrame,
    model_id: str,
    catalog: str | None,
    schema: str | None,
) -> list[dict[str, str]]:
    if not model_id or not catalog or not schema:
        return []

    scoped = access_matrix_df[
        (access_matrix_df["catalog"] == str(catalog))
        & (access_matrix_df["schema"] == str(schema))
        & (access_matrix_df["model_id"] == str(model_id))
    ]
    options: list[dict[str, str]] = []
    seen_report_ids: set[str] = set()

    if not scoped.empty:
        deduped = (
            scoped[["report_id", "report_name"]]
            .dropna(subset=["report_id"])
            .drop_duplicates(subset=["report_id"], keep="first")
            .sort_values(by=["report_name", "report_id"])
        )
        for _, row in deduped.iterrows():
            report_id = str(row["report_id"])
            seen_report_ids.add(report_id)
            label = str(row["report_name"]).strip() if pd.notna(row["report_name"]) else ""
            options.append({"label": label or report_id, "value": report_id})

    # Fallback: if matrix is stale or missing report rows, derive options from loaded report YAMLs.
    for report_id, report in REPORT_DEFS.items():
        if report_id in seen_report_ids:
            continue
        resolved_model = resolve_report_model_id(report, str(catalog), str(schema))
        if resolved_model != str(model_id):
            continue
        options.append({"label": str(report.get("name") or report_id), "value": report_id})

    options.sort(key=lambda o: (str(o.get("label") or "").lower(), str(o.get("value") or "")))
    return options


def _report_request(
    mv_def: MetricViewDef,
    report: dict[str, Any],
    max_rows: int | None,
    column_state: list[dict[str, Any]] | None = None,
    extra_filters: dict[str, dict[str, list] | list] | None = None,
) -> OlapQueryRequest:
    all_dim_names = {f.name for f in mv_def.dimension_fields}
    all_measure_names = {f.name for f in mv_def.measures}
    measure_by_lower = {f.name.lower(): f.name for f in mv_def.measures}
    measure_label_by_lower = {f.label.lower(): f.name for f in mv_def.measures if f.label}

    def _norm_token(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

    # Build group lookups so report YAML can reference dimensions by
    # display name ("Dim Customer") or alias-like names ("dim_customer").
    groups_by_name: dict[str, list[str]] = {}
    group_key_field: dict[str, str] = {}
    group_name_by_norm: dict[str, str] = {}
    for f in mv_def.dimension_fields:
        groups_by_name.setdefault(f.group_name, []).append(f.name)
        if f.field_type == "dimension_key" and f.group_name not in group_key_field:
            group_key_field[f.group_name] = f.name
        group_name_by_norm.setdefault(_norm_token(f.group_name), f.group_name)

    def _resolve_group_name(raw_name: str) -> str | None:
        key = _norm_token(raw_name)
        if not key:
            return None
        if key in group_name_by_norm:
            return group_name_by_norm[key]
        # Fallback: match by normalized field name to infer its group.
        for f in mv_def.dimension_fields:
            if _norm_token(f.name) == key:
                return f.group_name
        return None

    def _defaults_from_report() -> tuple[list[str], list[str]]:
        # Prefer explicit report.dimension_fields when initializing from report.
        rows: list[str] = []

        # Per-group field lists from report.dimension_fields.
        # Keys may be UI group labels ("Dim Customer") or aliases ("dim_customer").
        by_dimension = report.get("dimension_fields") or {}
        if isinstance(by_dimension, dict):
            for group_name, configured_fields in by_dimension.items():
                if not isinstance(configured_fields, list):
                    continue

                resolved_group = _resolve_group_name(str(group_name))
                valid = set(groups_by_name.get(resolved_group or "", []))

                for field in configured_fields:
                    if not isinstance(field, str):
                        continue
                    field_name = field.strip()
                    if not field_name or field_name not in all_dim_names:
                        continue

                    # If group resolved, keep fields within that group.
                    if valid and field_name not in valid:
                        continue

                    rows.append(field_name)

        # Backward-compatible fallback: if no dimension_fields are configured,
        # derive rows from report.dimensions.
        if not rows:
            for raw_dim in (report.get("dimensions") or []):
                if not isinstance(raw_dim, str):
                    continue

                dim_name = raw_dim.strip()
                if not dim_name:
                    continue

                if dim_name in all_dim_names:
                    rows.append(dim_name)
                    continue

                resolved_group = _resolve_group_name(dim_name)
                if not resolved_group:
                    continue

                group_fields = groups_by_name.get(resolved_group, [])
                first_non_key = next(
                    (
                        f.name
                        for f in mv_def.dimension_fields
                        if f.group_name == resolved_group and f.field_type != "dimension_key"
                    ),
                    None,
                )
                if first_non_key:
                    rows.append(first_non_key)
                elif group_fields:
                    rows.append(group_fields[0])

        rows = list(dict.fromkeys(rows))

        # Metrics / keyfigures
        metrics: list[str] = []
        for entry in (report.get("keyfigures") or []):
            metric_name: str | None = None
            if isinstance(entry, str):
                parsed = _parse_keyfigure_expr(entry)
                ref = parsed if parsed else entry
                metric_name = (
                    ref if ref in all_measure_names
                    else measure_by_lower.get(ref.lower())
                    or measure_label_by_lower.get(ref.lower())
                )
            elif isinstance(entry, dict):
                raw_name = str(entry.get("name") or entry.get("metric") or "").strip()
                metric_name = (
                    raw_name if raw_name in all_measure_names
                    else measure_by_lower.get(raw_name.lower())
                    or measure_label_by_lower.get(raw_name.lower())
                )
            if metric_name and metric_name not in metrics:
                metrics.append(metric_name)

        return rows, metrics

    if column_state:
        request = build_request_from_grid_state(column_state, max_rows)
        rows = list(request.rows or [])
        metrics = list(request.metrics or [])
        default_rows, default_metrics = _defaults_from_report()
        if not rows:
            rows = default_rows
        if not metrics:
            metrics = default_metrics
    else:
        rows, metrics = _defaults_from_report()

    # Filters
    all_filterable = mv_def.all_field_names
    filters: dict[str, dict[str, list] | list] = {
        field: spec
        for field, spec in (report.get("filters") or {}).items()
        if field in all_filterable
    }

    if isinstance(extra_filters, dict):
        for field, spec in extra_filters.items():
            if field in all_filterable:
                filters[field] = spec

    return OlapQueryRequest(rows=rows, metrics=metrics, filters=filters, max_rows=max_rows)


def _build_report_default_column_state(
    mv_def: MetricViewDef,
    report: dict[str, Any],
    max_rows: int | None,
) -> list[dict[str, Any]]:
    """Build initial AG Grid columnState from report defaults."""
    defaults_req = _report_request(mv_def, report, max_rows, column_state=None)
    row_fields = set(defaults_req.rows or [])

    state: list[dict[str, Any]] = []
    for field in mv_def.fields:
        is_row = field.name in row_fields
        state.append(
            {
                "colId": field.name,
                "rowGroup": is_row,
                "hide": not is_row,
                "pivot": False,
            }
        )
    return state


def list_catalog_names() -> list[str]:
    configured_catalogs = _parse_csv_env("PYDASH_APP_REPORTING_CATALOGS")
    if configured_catalogs:
        filtered = _exclude_information_schema(configured_catalogs)
        LOGGER.info("Using configured reporting catalogs from env: %s", filtered)
        return filtered

    queries = [
        "SHOW CATALOGS",
        "SELECT catalog_name FROM system.information_schema.catalogs",
    ]

    for sql in queries:
        try:
            df = _run_sql(sql)
        except Exception:
            LOGGER.info("Catalog listing query failed: %s", sql)
            continue

        names = sorted(set(_exclude_information_schema(_extract_first_non_null_str(df))))
        if names:
            return names

    return []


def list_schema_names(catalog: str) -> list[str]:
    LOGGER.info("Listing schemas for catalog: %s", catalog)
    namespace = _qualified_ident(catalog)
    queries = [
        f"SHOW SCHEMAS IN {namespace}",
        (
            "SELECT schema_name "
            f"FROM {_qualified_ident(catalog, 'information_schema', 'schemata')}"
        ),
    ]

    for sql in queries:
        try:
            df = _run_sql(sql, default_catalog=catalog)
        except Exception as e:
            LOGGER.warning("Schema listing query failed for %s: %s", catalog, sql)
            LOGGER.warning("Schema listing query error", exc_info=e)
            continue

        if df.empty:
            continue

        lower_to_actual = {str(c).strip().lower(): c for c in df.columns}
        preferred = [
            lower_to_actual.get("database_name"),
            lower_to_actual.get("namespace"),
            lower_to_actual.get("schema_name"),
        ]
        names: list[str] = []
        for col in preferred:
            if col:
                names.extend(
                    str(v).strip()
                    for v in df[col].tolist()
                    if pd.notna(v) and str(v).strip()
                )
                if names:
                    break

        if not names:
            names = _extract_first_non_null_str(df)

        names = _exclude_non_reporting_schemas(names)

        if names:
            return sorted(set(names))

    return []


def get_catalog_dropdown_options() -> list[dict[str, str]]:
    return [{"label": name, "value": name} for name in list_catalog_names()]


def get_schema_dropdown_options(catalog: str | None) -> list[dict[str, str]]:
    if not catalog:
        return []
    return [{"label": name, "value": name} for name in list_schema_names(catalog)]


def _qualified_ident(*parts: str | None) -> str:
    return ".".join(_quoted_ident(p) for p in parts if p)


def _parse_qualified_name(value: str) -> tuple[str | None, str | None, str]:
    cleaned = str(value).strip()
    if not cleaned:
        raise ValueError("Empty qualified name")

    tokens = [part.strip().strip("`") for part in cleaned.split(".") if part.strip()]
    if len(tokens) == 3:
        return tokens[0], tokens[1], tokens[2]
    if len(tokens) == 2:
        return None, tokens[0], tokens[1]
    if len(tokens) == 1:
        return None, None, tokens[0]
    raise ValueError(f"Invalid qualified name: {value}")


def _parse_expr_reference(expr: str) -> tuple[str, str] | None:
    token = str(expr).strip().strip("`")
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)", token)
    if not match:
        return None
    return match.group(1), match.group(2)


def _yaml_safe_load(text: str) -> Any:
    yaml_mod = importlib.import_module("yaml")
    return yaml_mod.safe_load(text)


def _yaml_safe_dump(value: Any) -> str:
    yaml_mod = importlib.import_module("yaml")
    return yaml_mod.safe_dump(value, sort_keys=False)


def _extract_yaml_from_dataframe(df: pd.DataFrame) -> str | None:
    if df.empty:
        return None

    lower_to_actual = {str(c).strip().lower(): c for c in df.columns}

    # Prefer structured describe output rows such as:
    # col_name='View Text' (or key='view_text') and YAML in a value-like column.
    key_col = (
        lower_to_actual.get("col_name")
        or lower_to_actual.get("key")
        or lower_to_actual.get("name")
    )
    value_candidates = [
        lower_to_actual.get("value"),
        lower_to_actual.get("data_type"),
        lower_to_actual.get("comment"),
        lower_to_actual.get("definition"),
    ]
    value_cols = [c for c in value_candidates if c]

    if key_col and value_cols:
        for _, row in df.iterrows():
            raw_key = row.get(key_col)
            if pd.isna(raw_key):
                continue

            key_text = str(raw_key).strip().lower().replace("_", " ")
            if key_text not in {"view text", "view definition", "metric view text", "definition"}:
                continue

            for value_col in value_cols:
                raw_value = row.get(value_col)
                if not isinstance(raw_value, str):
                    continue
                text = raw_value.strip()
                if not text:
                    continue
                if "source:" in text and ("dimensions:" in text or "measures:" in text):
                    return text
                # If key identifies view text, return non-empty payload as-is.
                return text

    yaml_col_names = [
        col
        for col in df.columns
        if "yaml" in str(col).lower() or "definition" in str(col).lower()
    ]

    for col in yaml_col_names + list(df.columns):
        for value in df[col].tolist():
            if not isinstance(value, str):
                continue
            text = value.strip()
            if not text:
                continue
            if "source:" in text and ("dimensions:" in text or "measures:" in text):
                return text

    return None


def _extract_yaml_from_tblproperties(df: pd.DataFrame) -> str | None:
    """Convert SHOW TBLPROPERTIES key/value rows into a metric-view YAML-like payload."""
    if df.empty:
        return None

    lower_to_actual = {str(c).strip().lower(): c for c in df.columns}
    key_col = lower_to_actual.get("key")
    value_col = lower_to_actual.get("value")
    if not key_col or not value_col:
        return None

    props: dict[str, str] = {}
    for _, row in df.iterrows():
        raw_key = row.get(key_col)
        raw_value = row.get(value_col)
        if pd.isna(raw_key):
            continue
        key = str(raw_key).strip()
        if not key:
            continue
        value = "" if pd.isna(raw_value) else str(raw_value).strip()
        props[key] = value

    def _parse_json_list(value: str) -> list:
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []

    from_name = props.get("metric_view.from.name", "")
    from_type = props.get("metric_view.from.type", "")
    joins = _parse_json_list(props.get("metric_view.joins", ""))
    dimensions = _parse_json_list(props.get("metric_view.dimensions", ""))
    measures = _parse_json_list(props.get("metric_view.measures", ""))

    if not from_name and not joins and not dimensions and not measures:
        return None

    payload: dict[str, Any] = {}
    if from_name:
        payload["source"] = {"name": from_name}
        if from_type:
            payload["source"]["type"] = from_type
    if joins:
        payload["joins"] = joins
    if dimensions:
        payload["dimensions"] = dimensions
    if measures:
        payload["measures"] = measures

    return _yaml_safe_dump(payload)


def _discover_metric_view_names(catalog: str, schema: str) -> list[str]:
    namespace = _qualified_ident(catalog, schema)
    escaped_schema = schema.replace("'", "''")
    queries = [
        (
            "SELECT table_name "
            f"FROM {_qualified_ident(catalog, 'information_schema', 'tables')} "
            f"WHERE table_schema = '{escaped_schema}' AND upper(table_type) IN ('METRIC_VIEW', 'METRIC VIEW')"
        ),
        f"SHOW VIEWS IN {namespace}",
    ]

    names: set[str] = set()
    for sql in queries:
        try:
            df = _run_sql(sql, default_catalog=catalog)
        except Exception:
            LOGGER.warning("Metric view discovery query failed: %s", sql)
            continue

        if df.empty:
            continue

        lower_to_actual = {str(c).strip().lower(): c for c in df.columns}
        name_col = (
            lower_to_actual.get("metric_view_name")
            or lower_to_actual.get("view_name")
            or lower_to_actual.get("table_name")
            or lower_to_actual.get("name")
            or df.columns[0]
        )
        is_metric_col = lower_to_actual.get("ismetric")

        if is_metric_col:
            for _, row in df.iterrows():
                raw_is_metric = row.get(is_metric_col)
                is_metric = False
                if isinstance(raw_is_metric, bool):
                    is_metric = raw_is_metric
                elif raw_is_metric is not None and not pd.isna(raw_is_metric):
                    is_metric = str(raw_is_metric).strip().lower() in {"true", "1", "yes", "y", "t"}

                if not is_metric:
                    continue

                raw = row.get(name_col)
                if pd.isna(raw):
                    continue
                metric_view_name = str(raw).strip().strip("`")
                if metric_view_name:
                    names.add(metric_view_name)
            if names:
                return sorted(names)
            continue

        for raw in df[name_col].tolist():
            if pd.isna(raw):
                continue
            metric_view_name = str(raw).strip().strip("`")
            if metric_view_name:
                names.add(metric_view_name)
        if names:
            return sorted(names)

    return sorted(names)


def _fetch_metric_view_yaml(catalog: str, schema: str, metric_view_name: str) -> str | None:
    qualified_name = _qualified_ident(catalog, schema, metric_view_name)
    queries = [
        #f"SHOW TBLPROPERTIES {qualified_name}",
        #f"DESCRIBE METRIC VIEW {qualified_name}",
        f"DESCRIBE EXTENDED {qualified_name}",
    ]

    for sql in queries:
        try:
            df = _run_sql(sql, default_catalog=catalog)
        except Exception:
            LOGGER.warning("Metric view describe query failed for %s: %s", metric_view_name, sql)
            continue

        yaml_text = (
            _extract_yaml_from_tblproperties(df) if sql.startswith("SHOW TBLPROPERTIES")
            else _extract_yaml_from_dataframe(df)
        )
        if yaml_text:
            return yaml_text

    return None


def _parse_metric_view_yaml(
    model_id: str,
    catalog: str,
    schema: str,
    metric_view_name: str,
    yaml_text: str,
) -> MetricViewDef:
    """
    Parse a Databricks metric-view YAML directly into a MetricViewDef.

    Mapping rules
    -------------
    joins[].on  → determines which dimension field is the key for each join.
    dimensions[].expr = alias.col  → dimension field on the metric view;
        if col == join dim_key → field_type="dimension_key", else "dimension_attr".
    If a join key is not explicitly present in dimensions[], synthesize one
        from joins[].on so each dimension group has a key field.
    dimensions[].expr = source.*  → skipped (fact-level, not exposed as dim field).
    measures[]  → field_type="measure".
    """
    raw = _yaml_safe_load(yaml_text) or {}

    joins = raw.get("joins", []) or []
    dimensions = raw.get("dimensions", []) or []
    measures_raw = raw.get("measures", []) or []

    # Build join map: alias → {dim_name, dim_key}
    join_info: dict[str, dict[str, str]] = {}
    for join in joins:
        alias = str(join.get("name") or "").strip()
        on_expr = str(join.get("on") or join.get('"on"') or "").strip()
        if not alias or not on_expr or "=" not in on_expr:
            continue
        left, right = [p.strip() for p in on_expr.split("=", 1)]
        left_ref = _parse_expr_reference(left)
        right_ref = _parse_expr_reference(right)
        if not left_ref or not right_ref:
            continue
        dim_side = None
        if left_ref[0] == "source" and right_ref[0] == alias:
            dim_side = right_ref
        elif right_ref[0] == "source" and left_ref[0] == alias:
            dim_side = left_ref
        if dim_side:
            join_info[alias] = {
                "dim_name": alias.replace("_", " ").title(),
                "dim_key": dim_side[1],
            }

    fields: list[MvField] = []
    seen: set[str] = set()

    for dim in dimensions:
        dim_name = str(dim.get("name") or "").strip()
        expr = str(dim.get("expr") or "").strip()
        label = str(dim.get("display_name") or dim_name or "").strip() or dim_name
        if not dim_name or dim_name in seen:
            continue
        ref = _parse_expr_reference(expr)
        if not ref:
            continue
        alias, col = ref
        if alias == "source":
            continue  # fact-level; not a dimension field on the metric view
        if alias not in join_info:
            continue
        ji = join_info[alias]
        field_type = "dimension_key" if col == ji["dim_key"] else "dimension_attr"
        seen.add(dim_name)
        fields.append(MvField(
            name=dim_name,
            label=label,
            field_type=field_type,
            group_name=ji["dim_name"],
            default_show=True,
        ))

    # Ensure each dimension group has a key field even when YAML dimensions
    # only listed attributes.
    for alias, ji in join_info.items():
        group_name = ji["dim_name"]
        has_group_key = any(
            f.group_name == group_name and f.field_type == "dimension_key"
            for f in fields
        )
        key_name = ji["dim_key"]
        if has_group_key or key_name in seen:
            continue

        seen.add(key_name)
        fields.append(MvField(
            name=key_name,
            label=key_name,
            field_type="dimension_key",
            group_name=group_name,
            default_show=False,
        ))

    for measure in measures_raw:
        m_name = str(measure.get("name") or "").strip()
        m_label = str(measure.get("display_name") or m_name or "").strip() or m_name
        if not m_name or m_name in seen:
            continue
        seen.add(m_name)
        fields.append(MvField(
            name=m_name,
            label=m_label,
            field_type="measure",
            group_name="Metrics",
            default_show=True,
        ))

    return MetricViewDef(
        model_id=model_id,
        catalog=catalog,
        schema=schema,
        metric_view_name=metric_view_name,
        display_name=f"Metric View {metric_view_name}",
        fields=fields,
    )


def load_metric_views(catalog: str, schema: str) -> dict[str, MetricViewDef]:
    """Discover and parse all metric views in a catalog/schema into MetricViewDef objects."""
    metric_view_names = _discover_metric_view_names(catalog, schema)
    if not metric_view_names:
        LOGGER.info("No metric views discovered in %s.%s", catalog, schema)
        return {}

    result: dict[str, MetricViewDef] = {}
    for mv_name in metric_view_names:
        yaml_text = _fetch_metric_view_yaml(catalog, schema, mv_name)
        if not yaml_text:
            STARTUP_WARNINGS.append(
                f"Could not read YAML for metric view {catalog}.{schema}.{mv_name}; skipping."
            )
            continue
        model_id = metric_view_model_id(catalog, schema, mv_name)
        try:
            result[mv_name] = _parse_metric_view_yaml(model_id, catalog, schema, mv_name, yaml_text)
        except Exception:
            STARTUP_WARNINGS.append(
                f"Failed parsing metric view {mv_name}; skipping."
            )
            LOGGER.info("Failed parsing metric view %s", mv_name, exc_info=True)

    LOGGER.info("Loaded %s metric view(s) from %s.%s", len(result), catalog, schema)
    return result


def _run_sql(sql: str, default_catalog: str | None = None) -> pd.DataFrame:
    LOGGER.info("Running SQL: %s", sql)
    return get_backend().execute_sql(sql, default_catalog=default_catalog)


REPORT_DEFS = load_report_definitions()

BACKEND_EXECUTOR: OlapDatabase | None = None

CATALOG_OPTIONS = [
    {"label": c, "value": c}
    for c in _exclude_information_schema(_parse_csv_env("PYDASH_APP_REPORTING_CATALOGS"))
]
INITIAL_CATALOG_VALUE = None
INITIAL_SCHEMA_OPTIONS: list[dict[str, str]] = []
INITIAL_SCHEMA_VALUE = None
INITIAL_MODEL_VALUE = None
INITIAL_MODEL_OPTIONS: list[dict[str, str]] = []


def _resolve_mv_def_by_model_id(model_id: str) -> MetricViewDef:
    """
    Resolve a metric-view definition by model_id without relying on global state.

    This is primarily used by the Flask /api/filter-values route, which runs
    outside Dash callback state and therefore cannot directly access dcc.Store.
    """
    _access_df, registry = _build_access_matrix()
    mv_def = registry.get(str(model_id))
    if mv_def is None:
        raise KeyError(f"Unknown model id: {model_id}")
    return mv_def


def get_backend() -> OlapDatabase:
    global BACKEND_EXECUTOR
    if BACKEND_EXECUTOR is None:
        BACKEND_EXECUTOR = create_databricks_backend()
    return BACKEND_EXECUTOR


# ---------------------------------------------------------------------------
# Store serialization / deserialization helpers
# ---------------------------------------------------------------------------

def _get_access_matrix_from_store(store_data) -> pd.DataFrame:
    """Reconstruct access-matrix DataFrame from dcc.Store list-of-dicts payload."""
    cols = ["catalog", "schema", "model_id", "metric_view", "report_id", "report_name"]
    if isinstance(store_data, list) and store_data:
        return pd.DataFrame(store_data, columns=cols)
    return pd.DataFrame(columns=cols)


def _get_mv_def_from_store(store_data, model_id: str) -> MetricViewDef | None:
    """Deserialize a single MetricViewDef from the metric-view-defs-store dict."""
    if not isinstance(store_data, dict) or not model_id:
        return None
    raw = store_data.get(str(model_id))
    if not isinstance(raw, dict):
        return None
    try:
        return metric_view_def_from_dict(raw)
    except Exception:
        LOGGER.warning("Failed to deserialize MetricViewDef for %s from store", model_id, exc_info=True)
        return None


def _model_exists_in_store(store_data, model_id: str | None) -> bool:
    """Check whether a model_id is present in the metric-view-defs-store."""
    return bool(model_id and isinstance(store_data, dict) and str(model_id) in store_data)


def execute_olap_request(model_id: str, request: OlapQueryRequest) -> pd.DataFrame:
    mv_def = _resolve_mv_def_by_model_id(model_id)
    return planner_execute_olap_request(get_backend(), mv_def, request)


def fetch_filter_values(model_id: str, field_name: str, max_values: int = 500) -> list:
    mv_def = _resolve_mv_def_by_model_id(model_id)
    return planner_fetch_filter_values(get_backend(), mv_def, field_name, max_values=max_values)


def get_flat_table(model_id: str) -> pd.DataFrame:
    request = OlapQueryRequest(max_rows=DEFAULT_MAX_ROWS)
    return execute_olap_request(model_id, request)


def build_default_request(mv_def: MetricViewDef, max_rows: int | None = None) -> OlapQueryRequest:
    default_rows = [f.name for f in mv_def.dimension_fields if f.default_show]
    default_metrics = [f.name for f in mv_def.measures if f.default_show]
    if not default_rows and not default_metrics:
        return OlapQueryRequest(max_rows=max_rows)
    return OlapQueryRequest(
        rows=list(dict.fromkeys(default_rows)),
        metrics=default_metrics,
        max_rows=max_rows,
    )


def get_default_view_table(model_id: str, max_rows: int | None = None) -> pd.DataFrame:
    mv_def = _resolve_mv_def_by_model_id(model_id)
    request = build_default_request(mv_def, max_rows=max_rows or DEFAULT_MAX_ROWS)
    return execute_olap_request(model_id, request)


def get_default_visible_fields(mv_def: MetricViewDef) -> set[str]:
    return {f.name for f in mv_def.fields if f.default_show}


def expand_visible_fields_with_dimension_keys(
    mv_def: MetricViewDef,
    visible_fields: set[str],
) -> set[str]:
    expanded = set(visible_fields)
    keys_by_group: dict[str, str] = {}
    for field in mv_def.dimension_fields:
        if field.field_type == "dimension_key" and field.group_name not in keys_by_group:
            keys_by_group[field.group_name] = field.name

    for group_name in mv_def.groups:
        group_fields = {field.name for field in mv_def.fields_for_group(group_name)}
        if expanded.intersection(group_fields):
            key_field = keys_by_group.get(group_name)
            if key_field:
                expanded.add(key_field)

    return expanded


def sanitize_max_rows(value) -> int | None:
    if value in (None, ""):
        return DEFAULT_MAX_ROWS
    try:
        max_rows = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ROWS
    return max_rows if max_rows > 0 else DEFAULT_MAX_ROWS


def get_logged_in_user() -> str:
    user = get_backend().current_user()
    LOGGER.info("Current user: %s", user)
    return user if user else "unknown"


def build_request_from_grid_state(column_state, max_rows_value) -> OlapQueryRequest:
    max_rows = sanitize_max_rows(max_rows_value)

    if not column_state:
        return OlapQueryRequest(max_rows=max_rows)
    
    row_fields: list[str] = []
    col_fields: list[str] = []

    for col_state in column_state:
        col_id = col_state.get("colId") or col_state.get("field")
        if not col_id:
            continue

        is_row_group = bool(col_state.get("rowGroup")) or col_state.get("rowGroupIndex") is not None
        is_pivot = bool(col_state.get("pivot")) or col_state.get("pivotIndex") is not None

        if is_row_group:
            row_fields.append(col_id)
            continue
        if is_pivot:
            col_fields.append(col_id)
            continue
        if not col_state.get("hide", False):
            row_fields.append(col_id)

    LOGGER.debug(f"Parsed grid state into request: row_fields={row_fields}, col_fields={col_fields}, max_rows={max_rows}")

    return OlapQueryRequest(
        rows=row_fields,
        columns=col_fields,
        metrics=[],
        filters={},
        max_rows=max_rows,
    )


def build_filters_from_filter_model(filter_model) -> dict[str, dict[str, list]]:
    filters: dict[str, dict[str, list]] = {}
    if not filter_model:
        return filters

    for field_name, cfg in filter_model.items():
        if not isinstance(cfg, dict):
            continue

        values = cfg.get("values")
        if isinstance(values, list) and values:
            filters[field_name] = {"in": values}
            continue

        single = cfg.get("filter")
        if single not in (None, ""):
            filters[field_name] = {"in": [single]}

    return filters


def get_model_dropdown_options() -> list[dict[str, str]]:
    return []


def get_allowed_filter_fields(mv_def: MetricViewDef) -> set[str]:
    return mv_def.all_field_names


def get_dimension_filter_fields(mv_def: MetricViewDef) -> list[str]:
    return sorted(f.name for f in mv_def.dimension_fields)


def get_dimension_dropdown_options(mv_def: MetricViewDef | None) -> list[dict[str, str]]:
    if mv_def is None:
        return []
    return [{"label": g, "value": g} for g in mv_def.groups]


def get_field_filter_dropdown_options(mv_def: MetricViewDef | None, dimension_name: str | None) -> list[dict[str, str]]:
    if mv_def is None or not dimension_name:
        return []
    return [
        {"label": f.label, "value": f.name}
        for f in mv_def.fields_for_group(dimension_name)
        if f.field_type != "measure"
    ]


def _split_csv_values(value: str | None) -> list[str]:
    parts = [p.strip() for p in (value or "").split(",")]
    return [p for p in parts if p]


def parse_manual_filters(filter_text: str | None, mv_def: MetricViewDef) -> tuple[dict[str, dict[str, list]], str | None]:
    text = (filter_text or "").strip()
    if not text:
        return {}, None

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"Invalid JSON at line {exc.lineno}, column {exc.colno}."

    if not isinstance(raw, dict):
        return {}, "Filter JSON must be an object: {\"field\": [values...]}"

    allowed = get_allowed_filter_fields(mv_def)
    parsed: dict[str, dict[str, list]] = {}

    for field, value in raw.items():
        if field not in allowed:
            return {}, f"Unknown filter field: {field}"

        # Backward-compatible simple form: {"field": [..]} or {"field": "x"}
        if not isinstance(value, dict):
            values = value if isinstance(value, list) else [value]
            cleaned = [v for v in values if v not in (None, "")]
            if cleaned:
                parsed[field] = {"in": cleaned}
            continue

        unknown_ops = [k for k in value.keys() if k not in ("in", "not_in")]
        if unknown_ops:
            return {}, f"Unsupported operator for {field}: {', '.join(unknown_ops)}"

        includes = value.get("in", [])
        excludes = value.get("not_in", [])

        includes = includes if isinstance(includes, list) else [includes]
        excludes = excludes if isinstance(excludes, list) else [excludes]

        includes_clean = [v for v in includes if v not in (None, "")]
        excludes_clean = [v for v in excludes if v not in (None, "")]

        overlap = set(map(str, includes_clean)).intersection(set(map(str, excludes_clean)))
        if overlap:
            return {}, f"Conflicting include/exclude values for {field}: {', '.join(sorted(overlap))}"

        spec: dict[str, list] = {}
        if includes_clean:
            spec["in"] = includes_clean
        if excludes_clean:
            spec["not_in"] = excludes_clean
        if spec:
            parsed[field] = spec

    return parsed, None


def filter_button_label(filter_count: int) -> str:
    return f"Applied filters ({filter_count})"


def startup_warning_style(hidden: bool) -> dict[str, str]:
    return {
        "display": "none" if hidden else "flex",
        "justifyContent": "space-between",
        "alignItems": "flex-start",
        "gap": "12px",
        "background": "#fffbeb",
        "border": "1px solid #f59e0b",
        "color": "#92400e",
        "padding": "10px 12px",
        "borderRadius": "8px",
        "marginBottom": "12px",
        "fontSize": "13px",
    }


def loading_indicator_card(text: str = "Updating..."):
    return html.Div(
        className="app-loading-card",
        children=[
            html.Div(className="app-loading-spinner"),
            html.Div(text, className="app-loading-text"),
        ],
    )

# ---------------------------------------------------------------------------
# Grid column-definition builders  (driven by MetricViewDef)
# ---------------------------------------------------------------------------

def _label(name: str) -> str:
    return name.replace("_", " ").title()


def build_column_defs(
    df: pd.DataFrame,
    mv_def: MetricViewDef,
    visible_fields: set[str] | None = None,
) -> list[dict]:
    """
    Build the full grouped column-def tree for AG Grid from a MetricViewDef.

    All dimension fields are always emitted so the columns panel is fully
    populated.  Visibility is controlled by `visible_fields`:
      - When provided (report-driven view): only those fields are visible.
      - When None: all fields shown (use model default_show for further tuning).
    Measures are only included when they appear in the SQL result.
    """
    defs: list[dict] = []
    df_cols: set[str] = set(df.columns)

    def _is_visible(field_name: str) -> bool:
        if visible_fields is None:
            return True
        return field_name in visible_fields

    # Group dimension fields by group_name (one AG Grid group per dimension)
    groups_seen: list[str] = []
    for f in mv_def.dimension_fields:
        if f.group_name not in groups_seen:
            groups_seen.append(f.group_name)

    for group_name in groups_seen:
        children: list[dict] = []
        for f in mv_def.fields_for_group(group_name):
            if f.field_type == "measure":
                continue
            children.append({
                "field": f.name,
                "headerName": f.label,
                "isDimension": True,
                "sortable": True,
                "filter": False,
                "resizable": True,
                "hide": not _is_visible(f.name),
                "enablePivot": False,
                "enableRowGroup": True,
            })
        if children:
            defs.append({"headerName": group_name, "children": children})

    # Measures — only for those returned by the current SQL result
    measure_children: list[dict] = []
    for f in mv_def.measures:
        if f.name in df_cols:
            measure_children.append({
                "field": f.name,
                "headerName": f.label,
                "isDimension": False,
                "sortable": True,
                "filter": False,
                "resizable": True,
                "type": "numericColumn",
                "enableValue": True,
                "hide": not _is_visible(f.name),
            })
    if measure_children:
        defs.append({"headerName": "Metrics", "children": measure_children})

    return defs


def build_grid_options() -> dict:
    return {
        "animateRows": True,
        "rowGroupPanelShow": "never",
        "pivotPanelShow": "never",
        "getContextMenuItems": {"function": "getCustomContextMenuItems(params)"},
        "groupDisplayType": "multipleColumns",
        "groupDefaultExpanded": -1,
        "groupHideOpenParents": True,
        "groupRemoveSingleChildren": True,
        "sideBar": {
            "position": "right",
            "toolPanels": [
                {
                    "id": "columns",
                    "labelDefault": "Columns",
                    "labelKey": "columns",
                    "iconKey": "columns",
                    "toolPanel": "agColumnsToolPanel",
                    "toolPanelParams": {
                        "suppressRowGroups": True,
                        "suppressPivotMode": True,
                        "suppressPivots": True,
                        "suppressValues": True,
                    },
                },
            ],
            "defaultToolPanel": "columns",
        },
    }


# ---------------------------------------------------------------------------
# Dash layout
# ---------------------------------------------------------------------------

app = Dash(__name__, assets_folder=str(BASE_DIR / "assets"))


@app.server.get("/api/filter-values")
def api_filter_values():
    model_id = flask_request.args.get("model_id", type=str)
    field_name = flask_request.args.get("field_name", type=str)
    max_values = flask_request.args.get("max_values", default=500, type=int)

    print(
        f"[api/filter-values] hit model_id={model_id} field_name={field_name} max_values={max_values}",
        flush=True,
    )

    LOGGER.debug(
        "Lazy filter values request received: model_id=%s field_name=%s max_values=%s",
        model_id,
        field_name,
        max_values,
    )

    if not model_id or not field_name:
        LOGGER.warning(
            "Lazy filter values request rejected: missing model_id or field_name (model_id=%s field_name=%s)",
            model_id,
            field_name,
        )
        return jsonify({"error": "model_id and field_name are required", "values": []}), 400

    try:
        values = fetch_filter_values(model_id, field_name, max_values=max_values)
        LOGGER.debug(
            "Lazy filter values response: model_id=%s field_name=%s count=%s",
            model_id,
            field_name,
            len(values),
        )
        return jsonify({"values": values})
    except Exception:
        LOGGER.warning(
            "Failed to fetch lazy filter values for model=%s field=%s",
            model_id,
            field_name,
            exc_info=True,
        )
        return jsonify({"values": []})


initial_df = pd.DataFrame()
initial_user = "Unknown user"

app.layout = html.Div(
    style={
        "fontFamily": "Arial, sans-serif",
        "padding": "16px",
        "width": "80vw",
        "maxWidth": "80vw",
        "margin": "0 auto",
    },
    children=[
        # Page-load trigger for single initialization callback
        dcc.Location(id="url", refresh=False),
        # Per-session stores (populated by initialize_session on every page load)
        dcc.Store(id="user-store",              storage_type="session", data={}),
        dcc.Store(id="access-matrix-store",     storage_type="session", data=[]),
        dcc.Store(id="metric-view-defs-store",  storage_type="session", data={}),
        dcc.Store(id="logged-in-user-store",    storage_type="session", data={"user": ""} ),
        dcc.Store(id="session-init-status-store", storage_type="session", data={"ready": False}),
        # Hidden store to track filter model changes
        dcc.Store(id="filter-change-trigger", data={"timestamp": 0, "filterModel": {}}),
        dcc.Store(id="column-change-trigger", data={"timestamp": 0, "columnState": []}),
        dcc.Store(id="manual-filter-store",   data={"timestamp": 0, "filters": {}}),
        dcc.Store(id="active-report-store",   data={"id": "", "timestamp": pd.Timestamp.utcnow().isoformat()}),        
        dcc.Store(id="field-filter-values-target", data={"mode": "include"}),
        dcc.Store(id="field-filter-modal-context", data={}),
        dcc.Input(id="field-filter-active-model", value="", style={"display": "none"}),
        dcc.Input(
            id="field-filter-active-field",
            value="",
            style={"display": "none"},
        ),
        html.Div(
            id="startup-warning-banner",
            children=[
                html.Div([html.Div(msg) for msg in STARTUP_WARNINGS], style={"flex": "1"}),
                html.Button(
                    "X",
                    id="dismiss-startup-warning-btn",
                    n_clicks=0,
                    style={
                        "border": "1px solid #f59e0b",
                        "background": "#fff7ed",
                        "color": "#92400e",
                        "borderRadius": "6px",
                        "padding": "2px 8px",
                        "cursor": "pointer",
                        "fontWeight": "700",
                        "lineHeight": "1.2",
                    },
                ),
            ],
            style=startup_warning_style(hidden=not STARTUP_WARNINGS),
        ),
        html.Div(
            id="session-loading-overlay",
            style={
                "position": "fixed",
                "inset": "0",
                "zIndex": 5000,
                "display": "flex",
                "alignItems": "center",
                "justifyContent": "center",
                "background": "rgba(255, 255, 255, 0.88)",
            },
            children=[loading_indicator_card("Updating...")],
        ),
        html.Div(
            style={
                "display": "flex",
                "justifyContent": "space-between",
                "alignItems": "center",
                "gap": "16px",
                "marginBottom": "12px",
            },
            children=[
                html.H2("General Slice & Dice", style={"margin": 0}),
                html.Div(
                    [
                        html.Span("Logged in user: ", style={"fontWeight": "600"}),
                        html.Span(initial_user, id="logged-in-user"),
                    ],
                    style={"color": "#374151"},
                ),
            ],
        ),
        html.Div(
            style={
                "marginBottom": "10px",
                "display": "flex",
                "gap": "16px",
                "alignItems": "end",
                "flexWrap": "wrap",
            },
            children=[
                html.Div(
                    style={"width": "300px"},
                    children=[
                        html.Div("Catalog", style={"fontWeight": "600", "marginBottom": "6px"}),
                        dcc.Dropdown(
                            id="catalog-selector",
                            options=CATALOG_OPTIONS,
                            value=INITIAL_CATALOG_VALUE,
                            clearable=False,
                        ),
                    ],
                ),
                html.Div(
                    style={"width": "300px"},
                    children=[
                        html.Div("Schema", style={"fontWeight": "600", "marginBottom": "6px"}),
                        dcc.Dropdown(
                            id="schema-selector",
                            options=INITIAL_SCHEMA_OPTIONS,
                            value=INITIAL_SCHEMA_VALUE,
                            clearable=False,
                        ),
                    ],
                ),
                html.Div(
                    style={"width": "300px"},
                    children=[
                        html.Div("Metric View", style={"fontWeight": "600", "marginBottom": "6px"}),
                        dcc.Dropdown(
                            id="model-selector",
                            options=INITIAL_MODEL_OPTIONS,
                            value=INITIAL_MODEL_VALUE,
                            clearable=False,
                        ),
                    ],
                ),
                html.Div(
                    style={"width": "300px"},
                    children=[
                        html.Div("Report", style={"fontWeight": "600", "marginBottom": "6px"}),
                        dcc.Dropdown(
                            id="report-selector",
                            options=[],
                            value=None,
                            clearable=False,
                        ),
                    ],
                ),
                html.Div(
                    style={"width": "300px", "paddingRight": "16px"},
                    children=[
                        html.Div("Max rows", style={"fontWeight": "600", "marginBottom": "6px"}),
                        dcc.Input(
                            id="max-rows-input",
                            type="number",
                            min=1,
                            step=1,
                            value=DEFAULT_MAX_ROWS,
                            debounce=True,
                            style={"width": "100%", "padding": "8px"},
                        ),
                    ],
                ),
                html.Div(
                    style={"display": "flex", "gap": "16px", "alignItems": "flex-end"},
                    children=[
                        html.Button(
                            "Static Filters",
                            id="open-report-filter-json-btn",
                            n_clicks=0,
                            style={
                                "height": "38px",
                                "padding": "0 14px",
                                "background": "#f3f4f6",
                                "border": "1px solid #d1d5db",
                            },
                        ),
                    ],
                ),
            ],
        ),
        html.Div(
            style={
                "marginBottom": "10px",
                "display": "flex",
                "gap": "16px",
                "alignItems": "end",
                "flexWrap": "wrap",
            },
            children=[
                html.Div(
                    style={"width": "300px"},
                    children=[
                        html.Div("Dimension", style={"fontWeight": "600", "marginBottom": "6px"}),
                        dcc.Dropdown(
                            id="field-filter-dimension-selector",
                            options=[],
                            value=None,
                            clearable=False,
                            style={"width": "100%"},
                        ),
                    ],
                ),
                html.Div(
                    style={"width": "300px"},
                    children=[
                        html.Div("Attribute", style={"fontWeight": "600", "marginBottom": "6px"}),
                        dcc.Dropdown(
                            id="field-filter-selector",
                            options=[],
                            value=None,
                            clearable=False,
                            style={"width": "100%"},
                        ),
                    ],
                ),
                html.Button(
                    "Set Filter",
                    id="apply-field-filter-btn",
                    n_clicks=0,
                    style={
                        "height": "38px",
                        "padding": "0 14px",
                        "background": "#f3f4f6",
                        "border": "1px solid #d1d5db",
                    },
                ),
                html.Button(
                    filter_button_label(0),
                    id="open-filter-json-btn",
                    n_clicks=0,
                    style={
                        "height": "38px",
                        "padding": "0 14px",
                        "background": "#f3f4f6",
                        "border": "1px solid #d1d5db",
                    },
                ),
                html.Button(
                    "Clear Filters",
                    id="clear-filters-btn",
                    n_clicks=0,
                    style={
                        "height": "38px",
                        "padding": "0 14px",
                        "background": "#f3f4f6",
                        "border": "1px solid #d1d5db",
                    },
                ),
            ],
        ),
        dcc.Textarea(
            id="server-filter-input",
            value="{}",
            style={"display": "none"},
        ),
        html.Div(
            id="filter-parse-message",
            style={"minWidth": "240px", "fontSize": "13px", "color": "#374151", "marginBottom": "12px"},
        ),
        html.Div(
            id="filter-json-modal",
            style={
                "display": "none",
                "position": "fixed",
                "inset": "0",
                "background": "rgba(0, 0, 0, 0.35)",
                "zIndex": 2000,
                "alignItems": "center",
                "justifyContent": "center",
            },
            children=[
                html.Div(
                    style={
                        "width": "760px",
                        "maxWidth": "95vw",
                        "background": "#fff",
                        "borderRadius": "10px",
                        "padding": "14px",
                        "boxSizing": "border-box",
                        "boxShadow": "0 10px 30px rgba(0, 0, 0, 0.2)",
                    },
                    children=[
                        html.Div(
                            style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": "8px"},
                            children=[
                                html.Div("Server Filters (JSON)", style={"fontWeight": "700"}),
                                html.Button(
                                    "X",
                                    id="close-filter-json-x-btn",
                                    n_clicks=0,
                                    style={
                                        "border": "1px solid #d1d5db",
                                        "background": "#fff",
                                        "borderRadius": "6px",
                                        "padding": "4px 8px",
                                        "cursor": "pointer",
                                        "fontWeight": "700",
                                    },
                                ),
                            ],
                        ),
                        dcc.Textarea(
                            id="server-filter-editor",
                            value="{}",
                            readOnly=True,
                            style={
                                "width": "100%",
                                "height": "240px",
                                "padding": "8px",
                                "fontFamily": "monospace",
                                "fontSize": "12px",
                                "boxSizing": "border-box",
                                "background": "#f9fafb",
                            },
                        ),
                        html.Div(
                            'Example: {"year": {"in": [2024]}, "region": {"not_in": ["APAC"]}}',
                            style={"fontSize": "12px", "color": "#6b7280", "marginTop": "6px"},
                        ),
                    ],
                )
            ],
        ),
        html.Div(
            id="report-filter-json-modal",
            style={
                "display": "none",
                "position": "fixed",
                "inset": "0",
                "background": "rgba(0, 0, 0, 0.35)",
                "zIndex": 2000,
                "alignItems": "center",
                "justifyContent": "center",
            },
            children=[
                html.Div(
                    style={
                        "width": "760px",
                        "maxWidth": "95vw",
                        "background": "#fff",
                        "borderRadius": "10px",
                        "padding": "14px",
                        "boxSizing": "border-box",
                        "boxShadow": "0 10px 30px rgba(0, 0, 0, 0.2)",
                    },
                    children=[
                        html.Div(
                            style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": "8px"},
                            children=[
                                html.Div("Report Filters (JSON)", style={"fontWeight": "700"}),
                                html.Button(
                                    "X",
                                    id="close-report-filter-json-x-btn",
                                    n_clicks=0,
                                    style={
                                        "border": "1px solid #d1d5db",
                                        "background": "#fff",
                                        "borderRadius": "6px",
                                        "padding": "4px 8px",
                                        "cursor": "pointer",
                                        "fontWeight": "700",
                                    },
                                ),
                            ],
                        ),
                        dcc.Textarea(
                            id="report-filter-editor",
                            value="{}",
                            readOnly=True,
                            style={
                                "width": "100%",
                                "height": "240px",
                                "padding": "8px",
                                "fontFamily": "monospace",
                                "fontSize": "12px",
                                "boxSizing": "border-box",
                                "background": "#f9fafb",
                            },
                        ),
                        html.Div(
                            "Read-only filters from the selected report YAML.",
                            style={"fontSize": "12px", "color": "#6b7280", "marginTop": "6px"},
                        ),
                    ],
                )
            ],
        ),
        html.Div(
            id="field-filter-modal",
            style={
                "display": "none",
                "position": "fixed",
                "inset": "0",
                "background": "rgba(0, 0, 0, 0.35)",
                "zIndex": 2100,
                "alignItems": "center",
                "justifyContent": "center",
            },
            children=[
                html.Div(
                    style={
                        "width": "680px",
                        "maxWidth": "95vw",
                        "background": "#fff",
                        "borderRadius": "10px",
                        "padding": "14px",
                        "boxSizing": "border-box",
                        "boxShadow": "0 10px 30px rgba(0, 0, 0, 0.2)",
                    },
                    children=[
                        html.Div(
                            style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": "10px"},
                            children=[
                                html.Div("Set Dimension Filter", id="field-filter-modal-title", style={"fontWeight": "700"}),
                                html.Button(
                                    "X",
                                    id="close-field-filter-modal-btn",
                                    n_clicks=0,
                                    style={
                                        "border": "1px solid #d1d5db",
                                        "background": "#fff",
                                        "borderRadius": "6px",
                                        "padding": "4px 8px",
                                        "cursor": "pointer",
                                        "fontWeight": "700",
                                    },
                                ),
                            ],
                        ),
                        html.Div(
                            style={"display": "flex", "gap": "12px", "alignItems": "end", "flexWrap": "wrap"},
                            children=[
                                html.Div(
                                    style={"width": "300px"},
                                    children=[
                                        html.Div("Include values (csv)", style={"fontWeight": "600", "marginBottom": "6px"}),
                                        dcc.Input(
                                            id="field-filter-modal-include-input",
                                            type="text",
                                            placeholder="e.g. EMEA, APAC",
                                            debounce=True,
                                            n_submit=0,
                                            style={"width": "100%", "padding": "8px", "boxSizing": "border-box"},
                                        ),
                                    ],
                                ),
                                html.Div(
                                    style={"width": "300px"},
                                    children=[
                                        html.Div("Exclude values (csv)", style={"fontWeight": "600", "marginBottom": "6px"}),
                                        dcc.Input(
                                            id="field-filter-modal-exclude-input",
                                            type="text",
                                            placeholder="e.g. Internal",
                                            debounce=True,
                                            n_submit=0,
                                            style={"width": "100%", "padding": "8px", "boxSizing": "border-box"},
                                        ),
                                    ],
                                ),
                            ],
                        ),
                        html.Div(
                            "Tip: Enter ? in include or exclude and press Apply to see valid values.",
                            style={"fontSize": "12px", "color": "#6b7280", "marginTop": "8px"},
                        ),
                        html.Div(
                            style={"display": "flex", "gap": "10px", "justifyContent": "flex-end", "marginTop": "12px"},
                            children=[
                                html.Button(
                                    "Clear Filter",
                                    id="clear-field-filter-modal-btn",
                                    n_clicks=0,
                                    style={
                                        "height": "36px",
                                        "padding": "0 14px",
                                        "background": "#fff",
                                        "border": "1px solid #d1d5db",
                                    },
                                ),
                                html.Button(
                                    "Cancel",
                                    id="cancel-field-filter-modal-btn",
                                    n_clicks=0,
                                    style={
                                        "height": "36px",
                                        "padding": "0 14px",
                                        "background": "#fff",
                                        "border": "1px solid #d1d5db",
                                    },
                                ),
                                html.Button(
                                    "Apply",
                                    id="apply-field-filter-modal-btn",
                                    n_clicks=0,
                                    style={
                                        "height": "36px",
                                        "padding": "0 14px",
                                        "background": "#f3f4f6",
                                        "border": "1px solid #d1d5db",
                                    },
                                ),
                            ],
                        ),
                    ],
                )
            ],
        ),
        html.Div(
            id="field-filter-values-modal",
            style={
                "display": "none",
                "position": "fixed",
                "inset": "0",
                "background": "rgba(0, 0, 0, 0.35)",
                "zIndex": 2200,
                "alignItems": "center",
                "justifyContent": "center",
            },
            children=[
                html.Div(
                    style={
                        "width": "760px",
                        "maxWidth": "95vw",
                        "background": "#fff",
                        "borderRadius": "10px",
                        "padding": "14px",
                        "boxSizing": "border-box",
                        "boxShadow": "0 10px 30px rgba(0, 0, 0, 0.2)",
                    },
                    children=[
                        html.Div(
                            style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": "8px"},
                            children=[
                                html.Div("Valid Values", id="field-filter-values-title", style={"fontWeight": "700"}),
                            ],
                        ),
                        html.Div(
                            style={
                                "maxHeight": "300px",
                                "overflowY": "auto",
                                "border": "1px solid #e5e7eb",
                                "borderRadius": "8px",
                                "padding": "8px",
                                "background": "#f9fafb",
                            },
                            children=[
                                dcc.Checklist(
                                    id="field-filter-values-checklist",
                                    options=[],
                                    value=[],
                                    inputStyle={"marginRight": "8px"},
                                    labelStyle={"display": "block", "marginBottom": "6px"},
                                )
                            ],
                        ),
                        html.Div(
                            style={"display": "flex", "justifyContent": "flex-end", "gap": "10px", "marginTop": "12px"},
                            children=[
                                html.Button(
                                    "Cancel",
                                    id="close-field-filter-values-modal-btn",
                                    n_clicks=0,
                                    style={
                                        "height": "36px",
                                        "padding": "0 14px",
                                        "background": "#fff",
                                        "border": "1px solid #d1d5db",
                                    },
                                ),
                                html.Button(
                                    "Use Selected",
                                    id="apply-field-filter-values-btn",
                                    n_clicks=0,
                                    style={
                                        "height": "36px",
                                        "padding": "0 14px",
                                        "background": "#f3f4f6",
                                        "border": "1px solid #d1d5db",
                                    },
                                ),
                            ],
                        ),
                        html.Div(
                            "Choose one or more values and click Use Selected.",
                            style={"fontSize": "12px", "color": "#6b7280", "marginTop": "8px"},
                        ),
                        html.Div(
                            id="field-filter-values-empty-note",
                            children="",
                            style={
                                "width": "100%",
                                "fontSize": "12px",
                                "color": "#6b7280",
                                "marginTop": "6px",
                            },
                        ),
                    ],
                )
            ],
        ),
        html.Div(
            style={
                "border": "1px solid #d1d5db",
                "borderRadius": "8px",
                "padding": "10px",
                "background": "#fff",
            },
            children=[
                dcc.Loading(
                    id="grid-loading-overlay",
                    fullscreen=True,
                    custom_spinner=loading_indicator_card("Updating..."),
                    children=dag.AgGrid(
                        id="olap-grid",
                        rowData=initial_df.to_dict("records"),
                        columnDefs=[],
                        eventListeners={
                            "filterChanged": ["onGridFilterChanged(params)"],
                            "columnVisible": ["onGridColumnStateChanged(params)"],
                            "columnPinned": ["onGridColumnStateChanged(params)"],
                            "columnMoved": ["onGridColumnStateChanged(params)"],
                            "columnRowGroupChanged": ["onGridColumnStateChanged(params)"],
                            "columnPivotChanged": ["onGridColumnStateChanged(params)"],
                            "columnValueChanged": ["onGridColumnStateChanged(params)"],
                        },
                        dangerously_allow_code=True,
                        defaultColDef={
                            "flex": 1,
                            "minWidth": 120,
                            "filter": False,      # off by default; dimension cols override per-column
                            "floatingFilter": False,
                        },
                        enableEnterpriseModules=True,
                        className="ag-theme-alpine",
                        dashGridOptions=build_grid_options(),
                        style={"height": "78vh", "width": "100%"},
                    ),
                )
            ],
        ),
    ],
)



@app.callback(
    Output("user-store", "data"),
    Output("access-matrix-store", "data"),
    Output("metric-view-defs-store", "data"),
    Output("logged-in-user-store", "data"),
    Output("session-init-status-store", "data"),
    Output("logged-in-user", "children"),
    Output("catalog-selector", "options"),
    Output("catalog-selector", "value"),
    Input("url", "href"),
    prevent_initial_call=False,
)
def initialize_session(href):
    """
    Single initialization callback — fires once on every browser page load.

    Loads all session state:
      - logged-in user (via SELECT current_user() using the request-time token)
      - access matrix (catalog/schema/model/report structure the user can see)
      - metric view definitions (field metadata for all accessible metric views)

    All results are stored in dcc.Store (session-scoped, per browser tab).
    The token is NEVER stored — it is re-read from the request header or env
    var on every SQL execution inside db.py.
    """

    LOGGER.info("initialize_session: browser page load, reinitializing session state")

    # --- Logged-in user ---------------------------------------------------
    try:
        logged_in_user = get_logged_in_user()
    except Exception:
        LOGGER.warning("initialize_session: failed to resolve current user", exc_info=True)
        logged_in_user = "unknown"
    LOGGER.info("initialize_session: logged_in_user=%s", logged_in_user)

    user_data = {"user": logged_in_user}

    # --- Access matrix (catalog/schema/metric-view/report rows) -----------
    access_matrix_df = pd.DataFrame(
        columns=["catalog", "schema", "model_id", "metric_view", "report_id", "report_name"]
    )
    metric_registry: dict[str, MetricViewDef] = {}
    try:
        access_matrix_df, metric_registry = _build_access_matrix()
        LOGGER.info("initialize_session: access matrix built with %s rows", len(access_matrix_df))
    except Exception:
        LOGGER.warning("initialize_session: failed to build access matrix", exc_info=True)

    access_matrix_data = access_matrix_df.to_dict("records")

    # --- Metric view definitions (serialized for per-session store) -------
    metric_view_defs_data: dict[str, dict] = {
        model_id: metric_view_def_to_dict(mvd)
        for model_id, mvd in metric_registry.items()
    }
    LOGGER.info("initialize_session: serialized %s metric view defs", len(metric_view_defs_data))

    # --- Catalog dropdown -------------------------------------------------
    catalog_options = get_catalog_dropdown_options_from_matrix(access_matrix_df)
    if not catalog_options:
        catalog_options = CATALOG_OPTIONS

    init_status = {"ready": True, "timestamp": pd.Timestamp.utcnow().isoformat()}
    return user_data, access_matrix_data, metric_view_defs_data, user_data, init_status, logged_in_user, catalog_options, None


@app.callback(
    Output("session-loading-overlay", "style"),
    Input("session-init-status-store", "data"),
    prevent_initial_call=False,
)
def toggle_session_loading_overlay(init_status):
    hidden = {
        "display": "none",
        "position": "fixed",
        "inset": "0",
        "zIndex": 5000,
        "alignItems": "center",
        "justifyContent": "center",
        "background": "rgba(255, 255, 255, 0.88)",
    }
    shown = {
        **hidden,
        "display": "flex",
    }

    init_ready = isinstance(init_status, dict) and bool(init_status.get("ready"))
    if init_ready:
        return hidden
    return shown


@app.callback(
    Output("schema-selector", "options"),
    Output("schema-selector", "value"),
    Input("catalog-selector", "value"),
    State("schema-selector", "value"),
    State("access-matrix-store", "data"),
    prevent_initial_call=True,
)
def on_catalog_change(catalog: str | None, current_schema: str | None, access_matrix_data):
    df = _get_access_matrix_from_store(access_matrix_data)
    schema_options = get_schema_dropdown_options_from_matrix(df, catalog)
    return schema_options, None


@app.callback(
    Output("model-selector", "options"),
    Output("model-selector", "value"),
    Output("server-filter-input", "value", allow_duplicate=True),
    Output("manual-filter-store", "data", allow_duplicate=True),
    Output("filter-parse-message", "children", allow_duplicate=True),
    Input("catalog-selector", "value"),
    Input("schema-selector", "value"),
    State("model-selector", "value"),
    State("access-matrix-store", "data"),
    prevent_initial_call=True,
)
def on_namespace_change_update_models(
    catalog: str | None,
    schema: str | None,
    current_model_id: str | None,
    access_matrix_data,
):
    empty_state = {"timestamp": pd.Timestamp.utcnow().isoformat(), "filters": {}}

    if not catalog or not schema:
        return [], None, "{}", empty_state, "Select a catalog and schema."

    df = _get_access_matrix_from_store(access_matrix_data)
    options = get_model_dropdown_options_from_matrix(df, catalog, schema)
    return options, None, "{}", empty_state, ""


@app.callback(
    Output("report-selector", "options"),
    Output("report-selector", "value"),
    Output("active-report-store", "data"),
    Input("catalog-selector", "value"),
    Input("schema-selector", "value"),
    Input("model-selector", "value"),
    State("report-selector", "value"),
    State("access-matrix-store", "data"),
    prevent_initial_call=True,
)
def on_model_or_namespace_or_report_change(
    catalog: str | None,
    schema: str | None,
    model_id: str | None,
    selected_report_id: str | None,
    access_matrix_data,
    prevent_initial_call=True,
):
    df = _get_access_matrix_from_store(access_matrix_data)
    options = get_report_dropdown_options(df, model_id or "", catalog, schema)

    values = {o["value"] for o in options}
    next_report_id = selected_report_id if selected_report_id in values else None
    active_report = {
        "id": next_report_id,
        "timestamp": pd.Timestamp.utcnow().isoformat(),
    }
    return options, next_report_id, active_report


@app.callback(
    Output("filter-json-modal", "style"),
    Output("server-filter-editor", "value"),
    Input("open-filter-json-btn", "n_clicks"),
    Input("close-filter-json-x-btn", "n_clicks"),
    State("manual-filter-store", "data"),
    prevent_initial_call=True,
)
def on_filter_json_modal_toggle(open_clicks, close_x_clicks, manual_filter_data):
    ctx = dash.callback_context
    triggered = ctx.triggered[0]["prop_id"] if ctx.triggered else ""

    hidden = {
        "display": "none",
        "position": "fixed",
        "inset": "0",
        "background": "rgba(0, 0, 0, 0.35)",
        "zIndex": 2000,
        "alignItems": "center",
        "justifyContent": "center",
    }
    shown = {
        **hidden,
        "display": "flex",
    }

    if triggered == "open-filter-json-btn.n_clicks":
        filters = {}
        if isinstance(manual_filter_data, dict):
            candidate = manual_filter_data.get("filters")
            if isinstance(candidate, dict):
                filters = candidate
        return shown, json.dumps(filters, indent=2)

    return hidden, no_update


@app.callback(
    Output("report-filter-json-modal", "style"),
    Output("report-filter-editor", "value"),
    Input("open-report-filter-json-btn", "n_clicks"),
    Input("close-report-filter-json-x-btn", "n_clicks"),
    State("active-report-store", "data"),
    State("report-selector", "value"),
    prevent_initial_call=True,
)
def on_report_filter_json_modal_toggle(open_clicks, close_x_clicks, active_report_data, report_selector_value):
    ctx = dash.callback_context
    triggered = ctx.triggered[0]["prop_id"] if ctx.triggered else ""

    hidden = {
        "display": "none",
        "position": "fixed",
        "inset": "0",
        "background": "rgba(0, 0, 0, 0.35)",
        "zIndex": 2000,
        "alignItems": "center",
        "justifyContent": "center",
    }
    shown = {
        **hidden,
        "display": "flex",
    }

    if triggered == "open-report-filter-json-btn.n_clicks":
        selected_report_id = ""
        if isinstance(active_report_data, dict):
            selected_report_id = str(active_report_data.get("id") or "")
        if not selected_report_id:
            selected_report_id = str(report_selector_value or "")

        report_filters = {}
        if selected_report_id and selected_report_id in REPORT_DEFS:
            report_filters = REPORT_DEFS[selected_report_id].get("filters") or {}
            if not isinstance(report_filters, dict):
                report_filters = {}

        return shown, json.dumps(report_filters, indent=2)

    return hidden, no_update


@app.callback(
    Output("server-filter-input", "value", allow_duplicate=True),
    Input("clear-filters-btn", "n_clicks"),
    prevent_initial_call=True,
)
def on_clear_filters_sync_input(clear_clicks):
    return "{}"


@app.callback(
    Output("field-filter-active-model", "value"),
    Output("field-filter-active-field", "value"),
    Output("field-filter-dimension-selector", "options"),
    Output("field-filter-dimension-selector", "value"),
    Output("field-filter-selector", "options"),
    Output("field-filter-selector", "value"),
    Input("model-selector", "value"),
    Input("field-filter-dimension-selector", "value"),
    State("field-filter-selector", "value"),
    State("metric-view-defs-store", "data"),
    prevent_initial_call=True,
)
def on_model_or_dimension_change_update_field_filter_options(
    model_id: str,
    current_dimension: str | None,
    current_field: str | None,
    metric_view_defs_data,
):
    mv_def = _get_mv_def_from_store(metric_view_defs_data, model_id)
    dim_options = get_dimension_dropdown_options(mv_def)
    dim_values = {o["value"] for o in dim_options}
    next_dimension = current_dimension if current_dimension in dim_values else (dim_options[0]["value"] if dim_options else None)

    field_options = get_field_filter_dropdown_options(mv_def, next_dimension)
    field_values = {o["value"] for o in field_options}
    next_field = current_field if current_field in field_values else (field_options[0]["value"] if field_options else None)
    return model_id, next_field or "", dim_options, next_dimension, field_options, next_field


@app.callback(
    Output("field-filter-active-model", "value", allow_duplicate=True),
    Output("field-filter-active-field", "value", allow_duplicate=True),
    Input("model-selector", "value"),
    Input("field-filter-selector", "value"),
    prevent_initial_call=True,
)
def sync_value_help_context(model_id: str, field_name: str | None):
    return model_id, field_name or ""


@app.callback(
    Output("field-filter-modal", "style"),
    Output("field-filter-modal-include-input", "value"),
    Output("field-filter-modal-exclude-input", "value"),
    Output("field-filter-modal-title", "children"),
    Output("field-filter-modal-context", "data"),
    Input("apply-field-filter-btn", "n_clicks"),
    Input("close-field-filter-modal-btn", "n_clicks"),
    Input("cancel-field-filter-modal-btn", "n_clicks"),
    Input("apply-field-filter-modal-btn", "n_clicks"),
    Input("clear-field-filter-modal-btn", "n_clicks"),
    Input("field-filter-modal-include-input", "n_submit"),
    Input("field-filter-modal-exclude-input", "n_submit"),
    State("field-filter-active-model", "value"),
    State("field-filter-dimension-selector", "value"),
    State("field-filter-dimension-selector", "options"),
    State("field-filter-active-field", "value"),
    State("field-filter-selector", "options"),
    State("field-filter-modal-include-input", "value"),
    State("field-filter-modal-exclude-input", "value"),
    State("server-filter-input", "value"),
    prevent_initial_call=True,
)
def on_set_filter_modal_toggle(
    open_clicks,
    close_clicks,
    cancel_clicks,
    apply_clicks,
    clear_clicks,
    include_submit,
    exclude_submit,
    model_id,
    dimension_name,
    dimension_options,
    field_name,
    field_options,
    include_csv,
    exclude_csv,
    current_filter_json,
):
    ctx = dash.callback_context
    triggered = ctx.triggered[0]["prop_id"] if ctx.triggered else ""

    hidden = {
        "display": "none",
        "position": "fixed",
        "inset": "0",
        "background": "rgba(0, 0, 0, 0.35)",
        "zIndex": 2100,
        "alignItems": "center",
        "justifyContent": "center",
    }
    shown = {
        **hidden,
        "display": "flex",
    }

    if triggered == "apply-field-filter-btn.n_clicks":
        if not field_name:
            return hidden, "", "", "Set Dimension Filter", {}

        field_label = str(field_name)
        dimension_label = str(dimension_name or "")

        if isinstance(dimension_options, list):
            for opt in dimension_options:
                if isinstance(opt, dict) and str(opt.get("value")) == str(dimension_name):
                    dimension_label = str(opt.get("label") or dimension_label)
                    break

        if isinstance(field_options, list):
            for opt in field_options:
                if isinstance(opt, dict) and str(opt.get("value")) == str(field_name):
                    field_label = str(opt.get("label") or field_name)
                    break

        if dimension_label:
            title = f"Set Filter for: {dimension_label} / {field_label}"
        else:
            title = f"Set Filter for: {field_label}"

        modal_context = {
            "model_id": str(model_id or ""),
            "dimension_name": str(dimension_name or ""),
            "dimension_label": str(dimension_label or ""),
            "field_name": str(field_name or ""),
            "field_label": str(field_label or ""),
        }

        include_values: list[str] = []
        exclude_values: list[str] = []
        try:
            current = json.loads((current_filter_json or "{}").strip())
            if isinstance(current, dict):
                spec = current.get(field_name) or {}
                if isinstance(spec, dict):
                    include_values = [str(v) for v in (spec.get("in") or []) if v not in (None, "")]
                    exclude_values = [str(v) for v in (spec.get("not_in") or []) if v not in (None, "")]
        except Exception:
            pass

        return shown, ", ".join(include_values), ", ".join(exclude_values), title, modal_context

    if triggered == "clear-field-filter-modal-btn.n_clicks":
        # Clear only the popup CSV inputs; do not mutate applied server filters.
        return shown, "", "", no_update, no_update

    if triggered in {
        "apply-field-filter-modal-btn.n_clicks",
        "field-filter-modal-include-input.n_submit",
        "field-filter-modal-exclude-input.n_submit",
    }:
        include_vals = _split_csv_values(include_csv)
        exclude_vals = _split_csv_values(exclude_csv)
        if "?" in include_vals or "?" in exclude_vals:
            return shown, no_update, no_update, no_update, no_update
        return hidden, no_update, no_update, no_update, no_update

    return hidden, no_update, no_update, no_update, no_update


@app.callback(
    Output("server-filter-input", "value", allow_duplicate=True),
    Input("apply-field-filter-modal-btn", "n_clicks"),
    Input("field-filter-modal-include-input", "n_submit"),
    Input("field-filter-modal-exclude-input", "n_submit"),
    State("field-filter-active-field", "value"),
    State("field-filter-modal-context", "data"),
    State("field-filter-modal-include-input", "value"),
    State("field-filter-modal-exclude-input", "value"),
    State("server-filter-input", "value"),
    prevent_initial_call=True,
)
def on_field_filter_apply_or_clear(
    apply_clicks,
    include_submit,
    exclude_submit,
    active_field_name,
    modal_context,
    include_csv,
    exclude_csv,
    current_filter_json,
):
    field_name = ""
    if isinstance(modal_context, dict):
        field_name = str(modal_context.get("field_name") or "")
    if not field_name:
        field_name = str(active_field_name or "")

    if not field_name:
        return no_update

    try:
        current = json.loads((current_filter_json or "{}").strip())
        if not isinstance(current, dict):
            current = {}
    except Exception:
        current = {}

    ctx = dash.callback_context
    triggered = ctx.triggered[0]["prop_id"] if ctx.triggered else ""

    include_vals = _split_csv_values(include_csv)
    exclude_vals = _split_csv_values(exclude_csv)

    if "?" in include_vals or "?" in exclude_vals:
        return no_update

    include_set = set(include_vals)
    exclude_vals = [v for v in exclude_vals if v not in include_set]

    if not include_vals and not exclude_vals:
        current.pop(field_name, None)
    else:
        spec: dict[str, list[str]] = {}
        if include_vals:
            spec["in"] = include_vals
        if exclude_vals:
            spec["not_in"] = exclude_vals
        current[field_name] = spec

    return json.dumps(current, indent=2)


@app.callback(
    Output("field-filter-values-modal", "style"),
    Output("field-filter-values-checklist", "options"),
    Output("field-filter-values-checklist", "value"),
    Output("field-filter-values-target", "data"),
    Output("field-filter-values-empty-note", "children"),
    Output("field-filter-values-title", "children"),
    Input("apply-field-filter-modal-btn", "n_clicks"),
    Input("field-filter-modal-include-input", "n_submit"),
    Input("field-filter-modal-exclude-input", "n_submit"),
    State("field-filter-modal-context", "data"),
    State("field-filter-modal-include-input", "value"),
    State("field-filter-modal-exclude-input", "value"),
    State("metric-view-defs-store", "data"),
    prevent_initial_call=True,
)
def on_field_filter_values_modal_toggle(
    apply_clicks,
    include_submit,
    exclude_submit,
    modal_context,
    include_csv,
    exclude_csv,
    metric_view_defs_data,
):
    ctx = dash.callback_context
    triggered = ctx.triggered[0]["prop_id"] if ctx.triggered else ""

    hidden = {
        "display": "none",
        "position": "fixed",
        "inset": "0",
        "background": "rgba(0, 0, 0, 0.35)",
        "zIndex": 2200,
        "alignItems": "center",
        "justifyContent": "center",
    }
    shown = {
        **hidden,
        "display": "flex",
    }

    include_vals = _split_csv_values(include_csv)
    exclude_vals = _split_csv_values(exclude_csv)
    if "?" not in include_vals and "?" not in exclude_vals:
        return hidden, no_update, no_update, no_update, no_update, no_update

    target_mode = "include" if "?" in include_vals else "exclude"

    model_id = ""
    field_name = ""
    dimension_label = ""
    field_label = ""
    if isinstance(modal_context, dict):
        model_id = str(modal_context.get("model_id") or "")
        field_name = str(modal_context.get("field_name") or "")
        dimension_label = str(modal_context.get("dimension_label") or "")
        field_label = str(modal_context.get("field_label") or "")

    if field_label and dimension_label:
        title = f"Valid Values for: {dimension_label} / {field_label}"
    elif field_label:
        title = f"Valid Values for: {field_label}"
    else:
        title = "Valid Values"

    model_known = _model_exists_in_store(metric_view_defs_data, model_id)
    if not model_known or not field_name:
        return shown, [], [], {"mode": target_mode}, "No values available.", title

    try:
        values = fetch_filter_values(model_id, field_name, max_values=500)
    except Exception:
        LOGGER.warning("Failed to fetch value-help values for field=%s", field_name, exc_info=True)
        values = []

    options = [{"label": str(v), "value": str(v)} for v in values if v not in (None, "")]
    note = "No values found for this field." if not options else ""
    return shown, options, [], {"mode": target_mode}, note, title


@app.callback(
    Output("field-filter-values-modal", "style", allow_duplicate=True),
    Output("field-filter-modal-include-input", "value", allow_duplicate=True),
    Output("field-filter-modal-exclude-input", "value", allow_duplicate=True),
    Input("apply-field-filter-values-btn", "n_clicks"),
    Input("close-field-filter-values-modal-btn", "n_clicks"),
    State("field-filter-values-checklist", "value"),
    State("field-filter-values-target", "data"),
    State("field-filter-modal-include-input", "value"),
    State("field-filter-modal-exclude-input", "value"),
    prevent_initial_call=True,
)
def on_field_filter_values_modal_apply_or_close(
    apply_clicks,
    close_clicks,
    selected_values,
    target_data,
    include_csv,
    exclude_csv,
):
    hidden = {
        "display": "none",
        "position": "fixed",
        "inset": "0",
        "background": "rgba(0, 0, 0, 0.35)",
        "zIndex": 2200,
        "alignItems": "center",
        "justifyContent": "center",
    }

    ctx = dash.callback_context
    triggered = ctx.triggered[0]["prop_id"] if ctx.triggered else ""
    if triggered == "close-field-filter-values-modal-btn.n_clicks":
        return hidden, no_update, no_update

    values = [str(v) for v in (selected_values or []) if str(v).strip()]

    def _replace_question_placeholder(csv_value: str | None, replacements: list[str]) -> str:
        tokens = [p.strip() for p in (csv_value or "").split(",")]
        has_placeholder = False
        output: list[str] = []
        for token in tokens:
            if not token:
                continue
            if token == "?":
                has_placeholder = True
                output.extend(replacements)
            else:
                output.append(token)
        if not has_placeholder:
            output.extend(replacements)
        return ", ".join(output)

    mode = "include"
    if isinstance(target_data, dict):
        mode = str(target_data.get("mode") or "include")

    if mode == "exclude":
        return hidden, include_csv or "", _replace_question_placeholder(exclude_csv, values)
    return hidden, _replace_question_placeholder(include_csv, values), exclude_csv or ""


@app.callback(
    Output("startup-warning-banner", "style"),
    Input("dismiss-startup-warning-btn", "n_clicks"),
    prevent_initial_call=True,
)
def on_dismiss_startup_warning(n_clicks):
    return startup_warning_style(hidden=True)


@app.callback(
    Output("manual-filter-store", "data"),
    Output("filter-parse-message", "children"),
    Output("filter-parse-message", "style"),
    Output("open-filter-json-btn", "children"),
    Input("clear-filters-btn", "n_clicks"),
    Input("server-filter-input", "value"),
    State("model-selector", "value"),
    State("metric-view-defs-store", "data"),
    prevent_initial_call=True,
)
def on_manual_filter_change(clear_clicks, filter_text, model_id: str, metric_view_defs_data):
    ctx = dash.callback_context
    triggered = ctx.triggered[0]["prop_id"] if ctx.triggered else ""

    if triggered == "clear-filters-btn.n_clicks":
        return (
            {"timestamp": pd.Timestamp.utcnow().isoformat(), "filters": {}},
            "Filters cleared.",
            {"minWidth": "240px", "fontSize": "13px", "color": "#065f46"},
            filter_button_label(0),
        )

    mv_def = _get_mv_def_from_store(metric_view_defs_data, model_id)
    if mv_def is None:
        return (
            {"timestamp": pd.Timestamp.utcnow().isoformat(), "filters": {}},
            "",
            {"minWidth": "240px", "fontSize": "13px", "color": "#374151"},
            filter_button_label(0),
        )

    parsed, error = parse_manual_filters(filter_text, mv_def)
    if error:
        return (
            no_update,
            error,
            {"minWidth": "240px", "fontSize": "13px", "color": "#b91c1c"},
            no_update,
        )

    return (
        {"timestamp": pd.Timestamp.utcnow().isoformat(), "filters": parsed},
        "",
        {"minWidth": "240px", "fontSize": "13px", "color": "#374151"},
        filter_button_label(len(parsed)),
    )


@app.callback(
    Output("olap-grid", "rowData"),
    Output("olap-grid", "columnDefs"),
    Input("catalog-selector", "value"),
    Input("schema-selector", "value"),
    Input("model-selector", "value"),
    Input("report-selector", "value"),
    Input("max-rows-input", "value"),
    Input("active-report-store", "data"),
    Input("olap-grid", "columnState"),
    Input("column-change-trigger", "data"),
    Input("filter-change-trigger", "data"),
    Input("manual-filter-store", "data"),
    State("metric-view-defs-store", "data"),
)
def on_grid_state_change(catalog_value,
                         schema_value,
                         model_id: str,
                         report_id,
                         max_rows_value,
                         active_report_data,
                         column_state,
                         column_trigger,
                         filter_trigger,
                         manual_filter_data,
                         metric_view_defs_data):
    """
    Single unified callback — fires on every grid state change:
      - model selector, max rows, column grouping/pivot, filter selections.

    Rebuilds column defs only when the model changes; always re-queries backend.
    All filtering and grouping is resolved server-side — AG Grid never filters locally.
    Session user and access matrix are managed by initialize_session; this callback
    reads metric view definitions from the per-session metric-view-defs-store.
    """
    ctx = dash.callback_context
    triggered = {t["prop_id"] for t in ctx.triggered}
    LOGGER.debug(f"Grid state change triggered by: {triggered}")

    # If critical context is missing, return early to avoid hitting the backend.
    if catalog_value is None or schema_value is None or model_id is None or \
       report_id is None or max_rows_value is None:
        return [], []

    #print(f"[on_grid_state_change] column_trigger={column_trigger}", flush=True)
    #print(f"[on_grid_state_change] filter_trigger={filter_trigger}", flush=True)
    #print(f"[on_grid_state_change] manual_filter_data={manual_filter_data}", flush=True)

    LOGGER.info("Grid state change: catalog=%s schema=%s model=%s report=%s max_rows=%s trigger=%s",
                 catalog_value, schema_value, model_id, report_id, max_rows_value, triggered)
    LOGGER.info("Grid state change: column state=%s", column_state)    
    effective_column_state = column_state
    if isinstance(column_trigger, dict):
        candidate = column_trigger.get("columnState")
        if isinstance(candidate, list):
            effective_column_state = candidate
            #print(f"[on_grid_state_change] using columnState from Store: {effective_column_state}", flush=True)

    filter_model = {}
    if isinstance(filter_trigger, dict):
        candidate = filter_trigger.get("filterModel")
        if isinstance(candidate, dict):
            filter_model = candidate
            #print(f"[on_grid_state_change] using filterModel from Store: {filter_model}", flush=True)

    manual_filters: dict[str, dict[str, list]] = {}
    if isinstance(manual_filter_data, dict):
        candidate = manual_filter_data.get("filters")
        if isinstance(candidate, dict):
            manual_filters = candidate

    if not catalog_value or not schema_value or not _model_exists_in_store(metric_view_defs_data, model_id):
        return [], []

    if report_id in (None, ""):
        return [], []

    selected_model = _get_mv_def_from_store(metric_view_defs_data, model_id)
    if selected_model is None:
        LOGGER.warning("on_grid_state_change: MetricViewDef not found in store for model_id=%s", model_id)
        return [], []

    max_rows = sanitize_max_rows(max_rows_value)
    rebuild_cols = (
        "model-selector.value" in triggered
        or "report-selector.value" in triggered
        or "active-report-store.data" in triggered
    )
    selected_report_id = ""
    if isinstance(active_report_data, dict):
        selected_report_id = str(active_report_data.get("id") or "")

    if not selected_report_id:
        selected_report_id = str(report_id or "")

    report_def = REPORT_DEFS.get(selected_report_id)

    runtime_filters: dict[str, dict[str, list] | list] = {}
    runtime_filters.update(build_filters_from_filter_model(filter_model))
    runtime_filters.update(manual_filters)

    if report_def is not None:
        use_grid_state_for_report = (
            "column-change-trigger.data" in triggered
            or "olap-grid.columnState" in triggered
            or "manual-filter-store.data" in triggered
        )
        report_column_state = effective_column_state if use_grid_state_for_report else None

        request = _report_request(
            selected_model,
            report_def,
            max_rows,
            column_state=report_column_state,
            extra_filters=runtime_filters,
        )
    else:
        request = (
            build_default_request(selected_model, max_rows)
            if rebuild_cols
            else build_request_from_grid_state(effective_column_state, max_rows)
        )
        merged_filters: dict[str, dict[str, list] | list] = dict(request.filters or {})
        merged_filters.update(runtime_filters)
        request.filters = merged_filters

    LOGGER.debug(
        "Grid state change: model=%s rows=%s pivots=%s filters=%s max_rows=%s trigger=%s raw_filter_model=%s",
        model_id,
        request.rows,
        request.columns,
        dict(request.filters),
        request.max_rows,
        triggered,
        filter_model,
    )

    result_df = planner_execute_olap_request(get_backend(), selected_model, request)

    if rebuild_cols:
        report_visible: set[str] | None = None
        if report_def is not None:
            requested_fields = set(request.rows or []).union(set(request.metrics or []))
            base_visible = requested_fields if requested_fields else get_default_visible_fields(selected_model)
            report_visible = base_visible
        new_col_defs = build_column_defs(result_df, selected_model, visible_fields=report_visible)
    else:
        new_col_defs = dash.no_update

    return result_df.to_dict("records"), new_col_defs




if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8050)
