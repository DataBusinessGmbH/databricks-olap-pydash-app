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
from typing import Callable, Protocol
from databricks.connect import DatabricksSession
import pandas as pd
from src.model import OlapModel

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
        mode=os.getenv("DATABRICKS_BACKEND_MODE", "spark").strip().lower(),
        server_hostname=os.getenv("DATABRICKS_SERVER_HOSTNAME"),
        http_path=os.getenv("DATABRICKS_HTTP_PATH"),
        access_token=os.getenv("DATABRICKS_TOKEN"),
        default_catalog=os.getenv("DATABRICKS_DEFAULT_CATALOG"),
        default_schema=os.getenv("DATABRICKS_DEFAULT_SCHEMA"),
    )
    LOGGER.info(
        "Databricks config loaded: mode=%s, host=%s, catalog=%s, schema=%s",
        cfg.mode,
        cfg.server_hostname,
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
    filters  – {attribute_name: [allowed_value, ...]} equality filters.
               Empty dict → no filtering.
    """
    rows: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    filters: dict[str, list] = field(default_factory=dict)
    max_rows: int | None = None

# ---------------------------------------------------------------------------
# Shared OLAP Processor (used by both Spark and SQL backends)
# ---------------------------------------------------------------------------
class OlapProcessor:
    """
    Shared OLAP processor that:
    - Builds SQL for OLAP operations
    - Executes common operations (`execute`, `current_user`, `filter_values`)
      via backend-specific `execute_sql()` function.
    """

    def __init__(
        self,
        model: OlapModel,
        config: DatabricksConnectionConfig,
        execute_sql_fn: Callable[[str], pd.DataFrame],
        backend_label: str,
    ) -> None:
        self._model = model
        self._config = config
        self._execute_sql = execute_sql_fn
        self._backend_label = backend_label

    def build_sql(self, request: OlapQueryRequest | None = None) -> str:
        """
        Build SQL query with only requested dimensions and metrics.
        
        If request is None, builds flat table with all dimensions (legacy behavior).
        If request has empty rows/columns, also builds with all dimensions (initial state).
        Otherwise, only includes dimensions in rows/columns and specified metrics.
        """
        fact_alias = "f"
        select_cols: list[str] = []
        group_by_cols: list[str] = []
        where_clauses: list[str] = []
        join_sql = []

        # Determine which dimension attributes are needed
        requested_cols = set()
        if request:
            requested_cols.update(request.rows)
            requested_cols.update(request.columns)

        filter_fields = set((request.filters or {}).keys()) if request else set()

        # If request is None or both rows/columns are empty, include all dimensions (flat table state)
        include_all_dims = request is None or (not request.rows and not request.columns)

        # Keep fact join keys as non-aggregated columns.
        if 1==2:
            for key in self._model.fact.join_keys:
                expr = f"{fact_alias}.{self._q(key)}"
                select_cols.append(f"{expr} AS {self._q(key)}")
                group_by_cols.append(expr)

        # Only include dimensions whose attributes are requested
        dim_idx = 0
        for dim in self._model.dimensions:
            # Check if any attribute from this dimension is requested
            dim_attrs_to_include = []
            dim_key_include = False            
            attr_names = {a.name for a in dim.attributes}
            dim_filter_attr_names = [a for a in attr_names if a in filter_fields]
            
            if include_all_dims:
                # Include all attributes (flat table or no drilldown yet)
                dim_attrs_to_include = [a.name for a in dim.attributes]
            else:
                # Only include attributes that are in requested columns
                dim_attrs_to_include = [a.name for a in dim.attributes if a.name in requested_cols]

            # Synthetic display key (alias of fact foreign key) — include if dimension is used
            if dim.dim_key in requested_cols or include_all_dims==True:
                display_key_expr = f"{fact_alias}.{self._q(dim.fact_key)}"
                select_cols.append(
                    f"{display_key_expr} AS {self._q(dim.dim_key)}"
                )
                group_by_cols.append(display_key_expr)      
                dim_key_include = True  # Flag to indicate we need to join this dimension

            # key filters on dim key/display key are applied against fact foreign key
            if request and request.filters:
                for key_field in (dim.dim_key, dim.display_key):
                    if key_field in request.filters and request.filters[key_field]:
                        where_sql = self._in_filter_sql(
                            f"{fact_alias}.{self._q(dim.fact_key)}",
                            request.filters[key_field],
                        )
                        if where_sql:
                            where_clauses.append(where_sql)

            # Only join dimension if it has attributes to include
            if not dim_attrs_to_include and not dim_key_include and not dim_filter_attr_names:
                continue
            
            dim_idx += 1
            d_alias = f"d{dim_idx}"
            join_sql.append(
                f"LEFT JOIN {self._qualified_table_name(dim)} {d_alias} "
                f"ON {fact_alias}.{self._q(dim.fact_key)} = {d_alias}.{self._q(dim.dim_key)}"
            )

            # Include only requested attributes
            for attr_name in dim_attrs_to_include:
                attr_expr = f"{d_alias}.{self._q(attr_name)}"
                select_cols.append(f"{attr_expr} AS {self._q(attr_name)}")
                group_by_cols.append(attr_expr)

            # filters on dimension attributes
            if request and request.filters:
                for attr_name in dim_filter_attr_names:
                    where_sql = self._in_filter_sql(
                        f"{d_alias}.{self._q(attr_name)}",
                        request.filters[attr_name],
                    )
                    if where_sql:
                        where_clauses.append(where_sql)

        # filters on fact join keys
        if request and request.filters:
            for key in self._model.fact.join_keys:
                if key in request.filters and request.filters[key]:
                    where_sql = self._in_filter_sql(
                        f"{fact_alias}.{self._q(key)}",
                        request.filters[key],
                    )
                    if where_sql:
                        where_clauses.append(where_sql)

        # Include only requested metrics (or all if request is None)
        requested_metrics = set(request.metrics) if request else set()
        for metric in self._model.metrics:
            if request is None or not requested_metrics or metric.name in requested_metrics:
                select_cols.append(
                    f"SUM({fact_alias}.{self._q(metric.name)}) AS {self._q(metric.name)}"
                )

        sql_parts = [
            "SELECT",
            "  " + ",\n  ".join(select_cols),
            f"FROM {self._qualified_table_name(self._model.fact)} {fact_alias}",
            *join_sql,
        ]

        if where_clauses:
            sql_parts.extend(
                [
                    "WHERE",
                    "  " + "\n  AND ".join(where_clauses),
                ]
            )

        if group_by_cols:
            sql_parts.extend(
                [
                    "GROUP BY",
                    "  " + ",\n  ".join(group_by_cols),
                ]
            )

        max_rows = self._resolve_max_rows(request)
        if max_rows is not None:
            sql_parts.append(f"LIMIT {max_rows}")

        return "\n".join(sql_parts)

    def build_distinct_values_sql(self, field_name: str, max_values: int = 500) -> str:
        """
        Build SQL to fetch distinct non-null values for one filter field.

        Supported fields:
        - Dimension attributes: distinct values via fact left join dimension.
        - Dimension dim_key/display_key: distinct fact foreign key aliased as field.
        - Fact join keys: distinct values from fact.
        """
        fact_alias = "f"
        max_vals = max_values if isinstance(max_values, int) and max_values > 0 else 500
        fact_table = self._qualified_table_name(self._model.fact)

        for dim in self._model.dimensions:
            attr_names = {a.name for a in dim.attributes}

            if field_name in attr_names:
                dim_alias = "d"
                attr_expr = f"{dim_alias}.{self._q(field_name)}"
                return "\n".join(
                    [
                        "SELECT DISTINCT",
                        f"  {attr_expr} AS {self._q(field_name)}",
                        f"FROM {fact_table} {fact_alias}",
                        (
                            f"LEFT JOIN {self._qualified_table_name(dim)} {dim_alias} "
                            f"ON {fact_alias}.{self._q(dim.fact_key)} = {dim_alias}.{self._q(dim.dim_key)}"
                        ),
                        f"WHERE {attr_expr} IS NOT NULL",
                        "ORDER BY 1",
                        f"LIMIT {max_vals}",
                    ]
                )

            if field_name == dim.dim_key or field_name == dim.display_key:
                fk_expr = f"{fact_alias}.{self._q(dim.fact_key)}"
                return "\n".join(
                    [
                        "SELECT DISTINCT",
                        f"  {fk_expr} AS {self._q(field_name)}",
                        f"FROM {fact_table} {fact_alias}",
                        f"WHERE {fk_expr} IS NOT NULL",
                        "ORDER BY 1",
                        f"LIMIT {max_vals}",
                    ]
                )

        if field_name in self._model.fact.join_keys:
            key_expr = f"{fact_alias}.{self._q(field_name)}"
            return "\n".join(
                [
                    "SELECT DISTINCT",
                    f"  {key_expr} AS {self._q(field_name)}",
                    f"FROM {fact_table} {fact_alias}",
                    f"WHERE {key_expr} IS NOT NULL",
                    "ORDER BY 1",
                    f"LIMIT {max_vals}",
                ]
            )

        raise ValueError(f"Unsupported field for filter values: {field_name}")

    def _qualified_table_name(self, table_def) -> str:
        parts = [
            getattr(table_def, "catalog", None) or self._config.default_catalog,
            getattr(table_def, "schema", None) or self._config.default_schema,
            table_def.table,
        ]
        qualified = ".".join([self._q(p) for p in parts if p])
        if not qualified:
            raise RuntimeError("Table name cannot be empty.")
        return qualified

    @staticmethod
    def _q(name: str) -> str:
        return f"`{name}`"

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

    def execute(self, request: OlapQueryRequest) -> pd.DataFrame:
        LOGGER.debug(
            "%s execute: rows=%s columns=%s metrics=%s filters=%s",
            self._backend_label,
            request.rows,
            request.columns,
            request.metrics,
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

    def __init__(self, model: OlapModel, config: DatabricksConnectionConfig | None = None) -> None:
        self._model = model
        self._config = config or load_databricks_config_from_env()
        self._processor = OlapProcessor(model, self._config, self.execute_sql, "Spark")
        self._flat: pd.DataFrame | None = None  # lazy cache
        self._spark = self._get_spark_session()
        LOGGER.info("Initialized Spark backend for model=%s", self._model.name)

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
    def __init__(self, model: OlapModel, config: DatabricksConnectionConfig | None = None) -> None:
        self._model = model
        self._config = config or load_databricks_config_from_env()
        self._processor = OlapProcessor(model, self._config, self.execute_sql, "SQL")
        self._flat: pd.DataFrame | None = None
        self._validate_config()
        LOGGER.info("Initialized SQL backend for model=%s", self._model.name)

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
            missing.append("DATABRICKS_SERVER_HOSTNAME")
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
    model: OlapModel, config: DatabricksConnectionConfig | None = None
) -> OlapDatabase:
    """
    Create backend using env/config mode.

    Modes:
      - spark (default): workspace Spark session
      - sql: Databricks SQL connector
    """
    cfg = config or load_databricks_config_from_env()
    LOGGER.info("Creating Databricks backend mode=%s for model=%s", cfg.mode, model.name)
    if cfg.mode == "sql":
        return DatabricksSqlBackend(model, cfg)
    return DatabricksSparkBackend(model, cfg)

