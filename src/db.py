"""
src/db.py
---------
Database layer for the OLAP application.

Design
------
The frontend never knows where data comes from.  It constructs an
OlapQueryRequest specifying which dimension attributes to use as rows,
which to pivot as columns, which metrics to aggregate, and any filter
predicates.  It then calls OlapDatabase.execute() and receives a plain
pandas DataFrame back.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import importlib
import logging
import os
import re
from typing import Callable, Protocol
from databricks.connect import DatabricksSession
import pandas as pd
from src.model import MetricViewDef

def _setup_logger() -> logging.Logger:
    level_name = os.getenv("APP_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logger = logging.getLogger(__name__)

    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )

    logger.setLevel(level)
    return logger
LOGGER = _setup_logger()


@dataclass
class DatabricksConnectionConfig:
    """Runtime configuration for Databricks access."""

    mode: str = "spark"  # spark | sql
    server_hostname: str | None = None
    http_path: str | None = None
    access_token: str | None = None
    default_catalog: str | None = None
    default_schema: str | None = None


def load_databricks_config_from_env() -> DatabricksConnectionConfig:

    """Load Databricks backend config from environment variables."""
    cfg = DatabricksConnectionConfig(
        mode=os.getenv("DATABRICKS_BACKEND_MODE").strip().lower(),
        server_hostname=f"https://{os.getenv('DATABRICKS_HOST')}/",
        http_path=os.getenv("DATABRICKS_HTTP_PATH"),
        access_token=os.getenv("DATABRICKS_TOKEN"),
        default_catalog=os.getenv("DATABRICKS_DEFAULT_CATALOG"),
        default_schema=os.getenv("DATABRICKS_DEFAULT_SCHEMA"),
    )
    LOGGER.info(
        "Databricks config loaded: mode=%s, host=%s, %s, catalog=%s, schema=%s",
        cfg.mode,
        cfg.server_hostname,
        f"****({len(cfg.access_token)})" if cfg.access_token else None,
        cfg.default_catalog,
        cfg.default_schema,
    )
    return cfg


# ---------------------------------------------------------------------------
# Query Request  (the generic contract the frontend places)
# ---------------------------------------------------------------------------
@dataclass
class OlapQueryRequest:
    """
    A model-level query: entirely in business terms, no SQL or file paths.

    rows     – dimension attribute names to use as row-group axes.
               Empty list → return the fully de-normalised flat table.
    columns  – dimension attribute names to pivot into column headers.
               Empty list → no pivot.
    metrics  – metric names to aggregate (must be defined in model.yaml).
               Empty list → return all metrics.
    filters  – {attribute_name: {"in": [...], "not_in": [...]}} filters.
               Legacy form {attribute_name: [...]} is still supported.
               Empty dict → no filtering.
    """
    rows: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    metric_aggs: dict[str, str] = field(default_factory=dict)
    filters: dict[str, dict[str, list] | list] = field(default_factory=dict)
    max_rows: int | None = None

# ---------------------------------------------------------------------------
# Shared OLAP Processor (used by both Spark and SQL backends)
# ---------------------------------------------------------------------------
class OlapProcessor:
    """
    Shared OLAP processor backed by a MetricViewDef.

    The metric view is queried directly as a single table — no joins to
    underlying dimension tables are performed.  Dimension attributes and
    measures are all columns on the metric view.
    """

    def __init__(
        self,
        mv_def: MetricViewDef,
        config: DatabricksConnectionConfig,
        execute_sql_fn: Callable[[str], pd.DataFrame],
        backend_label: str,
    ) -> None:
        self._mv = mv_def
        self._config = config
        self._execute_sql = execute_sql_fn
        self._backend_label = backend_label

    def build_sql(self, request: OlapQueryRequest | None = None) -> str:
        """
        Build a SELECT … FROM <metric_view> … GROUP BY … query.

        - When request is None or rows/columns are empty: include all fields.
        - Otherwise: include only the requested dimension fields and metrics.
        - All filters apply directly on metric-view columns.
        """
        mv = self._mv
        fact_alias = "f"
        q = self._q
        select_cols: list[str] = []
        group_by_cols: list[str] = []
        where_clauses: list[str] = []

        requested_rows: set[str] = set()
        requested_metrics: set[str] = set()
        if request:
            requested_rows.update(request.rows or [])
            requested_rows.update(request.columns or [])
            requested_metrics.update(request.metrics or [])

        include_all = request is None or (not request.rows and not request.columns)

        LOGGER.info(
            "Building SQL for request: rows=%s metrics=%s filters=%s",
            requested_rows or "(all)",
            requested_metrics or "(all)",
            list((request.filters or {}).keys()) if request else None,
        )

        # Dimension fields — all come directly from the metric view
        for f in mv.dimension_fields:
            if include_all or f.name in requested_rows:
                expr = f"{fact_alias}.{q(f.name)}"
                select_cols.append(f"{expr} AS {q(f.name)}")
                group_by_cols.append(expr)
            if request and request.filters and f.name in request.filters:
                where_sql = self._filter_sql(
                    f"{fact_alias}.{q(f.name)}", request.filters[f.name]
                )
                if where_sql:
                    where_clauses.append(where_sql)

        # Measures — use MEASURE() aggregation syntax
        for f in mv.measures:
            if include_all or not requested_metrics or f.name in requested_metrics:
                select_cols.append(f"MEASURE({q(f.name)}) AS {q(f.name)}")
            if request and request.filters and f.name in request.filters:
                where_sql = self._filter_sql(
                    f"{fact_alias}.{q(f.name)}", request.filters[f.name]
                )
                if where_sql:
                    where_clauses.append(where_sql)

        sql_parts = [
            "SELECT",
            "  " + ",\n  ".join(select_cols),
            f"FROM {self._qualified_mv_name()} {fact_alias}",
        ]

        if where_clauses:
            unique_clauses = list(dict.fromkeys(where_clauses))
            sql_parts += ["WHERE", "  " + "\n  AND ".join(unique_clauses)]

        if group_by_cols:
            sql_parts += ["GROUP BY", "  " + ",\n  ".join(group_by_cols)]

        max_rows = self._resolve_max_rows(request)
        if max_rows is not None:
            sql_parts.append(f"LIMIT {max_rows}")

        return "\n".join(sql_parts)

    def build_distinct_values_sql(self, field_name: str, max_values: int = 500) -> str:
        """
        Distinct non-null values for one field, queried directly from the metric view.
        All dimension attributes and keys are columns on the metric view object.
        """
        if field_name not in self._mv.all_field_names:
            raise ValueError(f"Unknown field for filter values: {field_name}")

        fact_alias = "f"
        max_vals = max_values if isinstance(max_values, int) and max_values > 0 else 500
        expr = f"{fact_alias}.{self._q(field_name)}"
        return "\n".join([
            "SELECT DISTINCT",
            f"  {expr} AS {self._q(field_name)}",
            f"FROM {self._qualified_mv_name()} {fact_alias}",
            f"WHERE {expr} IS NOT NULL",
            "ORDER BY 1",
            f"LIMIT {max_vals}",
        ])

    def _qualified_mv_name(self) -> str:
        mv = self._mv
        parts = [p for p in [mv.catalog, mv.schema, mv.metric_view_name] if p]
        if not parts:
            raise RuntimeError("MetricViewDef has no table name.")
        return ".".join(self._q(p) for p in parts)

    @staticmethod
    def _q(name: str) -> str:
        return f"`{name}`"

    def _normalize_metric_expr(self, expr: str, fact_alias: str) -> str:
        """Map metric-view source alias references to the generated fact alias."""
        text = str(expr)

        def repl(match: re.Match[str]) -> str:
            return f"{fact_alias}.{self._q(match.group(1))}"

        patterns = [
            r"`source`\.`([^`]+)`",
            r"`source`\.([A-Za-z_][A-Za-z0-9_]*)",
            r"\bsource\.`([^`]+)`",
            r"\bsource\.([A-Za-z_][A-Za-z0-9_]*)",
        ]
        for pattern in patterns:
            text = re.sub(pattern, repl, text)

        return text

    @staticmethod
    def _resolve_max_rows(request: OlapQueryRequest | None) -> int | None:
        if request is None or request.max_rows is None:
            return None

        try:
            max_rows = int(request.max_rows)
        except (TypeError, ValueError):
            return None

        return max_rows if max_rows > 0 else None

    @staticmethod
    def _sql_literal(value) -> str | None:
        if value is None:
            return None
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"

    @classmethod
    def _in_filter_sql(cls, expr: str, allowed_values: list) -> str | None:
        literals = [cls._sql_literal(v) for v in (allowed_values or [])]
        literals = [v for v in literals if v is not None]
        if not literals:
            return None
        # Compare as strings to match AG Grid filter model values consistently
        return f"CAST({expr} AS STRING) IN ({', '.join(literals)})"

    @classmethod
    def _not_in_filter_sql(cls, expr: str, excluded_values: list) -> str | None:
        literals = [cls._sql_literal(v) for v in (excluded_values or [])]
        literals = [v for v in literals if v is not None]
        if not literals:
            return None
        # Normalize NULL to empty string before applying NOT IN filters.
        return f"(COALESCE(CAST({expr} AS STRING), '') NOT IN ({', '.join(literals)}))"

    @classmethod
    def _normalize_filter_spec(cls, spec) -> dict[str, list]:
        # Backward-compatible: list means include list.
        if isinstance(spec, list):
            return {"in": spec}
        if not isinstance(spec, dict):
            return {}
        includes = spec.get("in", [])
        excludes = spec.get("not_in", [])
        if not isinstance(includes, list):
            includes = [includes]
        if not isinstance(excludes, list):
            excludes = [excludes]
        return {
            "in": [v for v in includes if v is not None],
            "not_in": [v for v in excludes if v is not None],
        }

    @classmethod
    def _filter_sql(cls, expr: str, spec) -> str | None:
        normalized = cls._normalize_filter_spec(spec)
        parts: list[str] = []

        in_sql = cls._in_filter_sql(expr, normalized.get("in", []))
        if in_sql:
            parts.append(in_sql)

        not_in_sql = cls._not_in_filter_sql(expr, normalized.get("not_in", []))
        if not_in_sql:
            parts.append(not_in_sql)

        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return "(" + " AND ".join(parts) + ")"

    def execute(self, request: OlapQueryRequest) -> pd.DataFrame:
        LOGGER.debug(
            "%s execute: rows=%s columns=%s metrics=%s metric_aggs=%s filters=%s",
            self._backend_label,
            request.rows,
            request.columns,
            request.metrics,
            request.metric_aggs,
            list((request.filters or {}).keys()),
        )
        sql = self.build_sql(request)        
        df = self._execute_sql(sql)
        LOGGER.debug("%s execute result shape=%s", self._backend_label, df.shape)
        return df

    def current_user(self) -> str:
        try:
            user_df = self._execute_sql("SELECT current_user() AS current_user")
            if not user_df.empty and "current_user" in user_df.columns:
                return str(user_df.iloc[0]["current_user"])
        except Exception:
            LOGGER.warning("Failed to resolve %s current user", self._backend_label, exc_info=True)
        return "Unknown user"

    def filter_values(self, field_name: str, max_values: int = 500) -> list:
        sql = self.build_distinct_values_sql(field_name, max_values)
        values_df = self._execute_sql(sql)
        if values_df.empty:
            return []
        return [v for v in values_df.iloc[:, 0].tolist() if pd.notna(v)]



# ---------------------------------------------------------------------------
# Protocol  (the interface any backend must satisfy)
# ---------------------------------------------------------------------------
class OlapDatabase(Protocol):
    """Any backend that can execute an OlapQueryRequest."""

    def execute(self, request: OlapQueryRequest) -> pd.DataFrame:
        """Return a DataFrame satisfying the request."""
        ...

    def flat_table(self) -> pd.DataFrame:
        """Return the fully joined, un-aggregated flat DataFrame."""
        ...

    def current_user(self) -> str:
        """Return the current backend user, or a safe fallback."""
        ...

    def filter_values(self, field_name: str, max_values: int = 500) -> list:
        """Return distinct non-null values for a filterable field."""
        ...

    def execute_sql(self, sql: str) -> pd.DataFrame:
        """Execute raw SQL and return result as DataFrame."""
        ...

# ---------------------------------------------------------------------------
# Databricks Spark Backend
# ---------------------------------------------------------------------------
class DatabricksSparkBackend:
    """
    Implements OlapDatabase using tables available in the Databricks workspace
    where the app is running.

    It reads model table metadata (catalog/schema/table), loads Spark tables,
    joins them, and returns pandas DataFrames to the frontend.
    """

    def __init__(self, mv_def: MetricViewDef, config: DatabricksConnectionConfig | None = None) -> None:
        self._mv = mv_def
        self._config = config or load_databricks_config_from_env()
        self._processor = OlapProcessor(mv_def, self._config, self.execute_sql, "Spark")
        self._flat: pd.DataFrame | None = None  # lazy cache
        self._spark = self._get_spark_session()
        LOGGER.info("Initialized Spark backend for model=%s", self._mv.model_id)
        current_user = self.current_user()
        LOGGER.info("Current_user=%s", current_user)
        

    # -- Public interface ----------------------------------------------------
    def execute(self, request: OlapQueryRequest) -> pd.DataFrame:
        return self._processor.execute(request)

    def execute_sql(self, sql: str) -> pd.DataFrame:
        LOGGER.info("Executing Spark SQL:\n%s", sql)
        return self._spark.sql(sql).toPandas()

    def current_user(self) -> str:
        return self._processor.current_user()

    def filter_values(self, field_name: str, max_values: int = 500) -> list:
        return self._processor.filter_values(field_name, max_values)

    # -- Private helpers -----------------------------------------------------
    @staticmethod
    def _get_spark_session():

        spark = DatabricksSession.builder.serverless().getOrCreate()
        if spark is None:
            raise RuntimeError("No active Spark session found in current runtime.")
        return spark


class DatabricksSqlBackend:
    """
    Databricks SQL Warehouse backend using `databricks-sql-connector`.
    """
    def __init__(self, mv_def: MetricViewDef, config: DatabricksConnectionConfig | None = None) -> None:
        self._mv = mv_def
        self._config = config or load_databricks_config_from_env()
        self._processor = OlapProcessor(mv_def, self._config, self.execute_sql, "SQL")
        self._flat: pd.DataFrame | None = None
        self._validate_config()
        LOGGER.info("Initialized SQL backend for model=%s", self._mv.model_id)

    def execute(self, request: OlapQueryRequest) -> pd.DataFrame:
        return self._processor.execute(request)

    def execute_sql(self, sql: str) -> pd.DataFrame:
        LOGGER.info("Executing SQL query:\n%s", sql)
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return pd.DataFrame(rows, columns=cols)
        except Exception as ex:
            LOGGER.error("SQL query failed:\n%s", sql, exc_info=True)
            raise RuntimeError("Failed to execute SQL query. See logs for details.") from ex
        finally:
            conn.close()

    def current_user(self) -> str:
        return self._processor.current_user()

    def filter_values(self, field_name: str, max_values: int = 500) -> list:
        return self._processor.filter_values(field_name, max_values)

    def _validate_config(self) -> None:
        missing = []
        if not self._config.server_hostname:
            missing.append("DATABRICKS_HOST")
        if not self._config.http_path:
            missing.append("DATABRICKS_HTTP_PATH")
        if not self._config.access_token:
            missing.append("DATABRICKS_TOKEN")
        if missing:
            LOGGER.error("SQL backend config missing env vars: %s", ", ".join(missing))
            raise RuntimeError(
                "Databricks SQL backend missing required env vars: " + ", ".join(missing)
            )

    def _connect(self):
        try:
            sql_mod = importlib.import_module("databricks.sql")
        except Exception as ex:
            LOGGER.exception("Failed to import databricks.sql connector")
            raise RuntimeError(
                "databricks-sql-connector is not installed. Install it to use SQL backend."
            ) from ex

        LOGGER.debug(
            "Opening Databricks SQL connection to host=%s http_path=%s",
            self._config.server_hostname,
            self._config.http_path,
        )
        return sql_mod.connect(
            server_hostname=self._config.server_hostname,
            http_path=self._config.http_path,
            access_token=self._config.access_token,
        )


def create_databricks_backend(
    mv_def: MetricViewDef, 
    config: DatabricksConnectionConfig | None = None
) -> OlapDatabase:
    """
    Create backend using env/config mode.

    Modes:
      - spark (default): workspace Spark session
      - sql: Databricks SQL connector
    """
    cfg = config or load_databricks_config_from_env()
    LOGGER.info("Creating Databricks backend mode=%s for model=%s", cfg.mode, mv_def.model_id)
    if cfg.mode == "sql":
        return DatabricksSqlBackend(mv_def, cfg)
    elif cfg.mode == "spark":
        return DatabricksSparkBackend(mv_def, cfg)
    else:
        raise RuntimeError(f"Unsupported Databricks backend mode: {cfg.mode}")

