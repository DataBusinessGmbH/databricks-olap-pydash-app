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

The OlapDatabase interface is intentionally abstract so that one backend
can be swapped for another (Databricks workspace Spark, Databricks SQL,
Snowflake) without touching app.py.

Public API
----------
  request = OlapQueryRequest(
      rows=["year", "quarter"],
      columns=["category"],
      metrics=["sales_amount"],
      filters={"region": ["North", "South"]},
  )
    db = CsvBackend(model)
  df: pd.DataFrame = db.execute(request)
"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import logging
import os
from typing import Protocol
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
# Shared SQL Builder (used by both Spark and SQL backends)
# ---------------------------------------------------------------------------

class OlapSqlBuilder:
    """
    Builds SQL queries for OLAP requests, independent of execution engine.
    Used by both DatabricksSparkBackend (executes via spark.sql) and 
    DatabricksSqlBackend (executes via SQL connector).
    """

    def __init__(self, model: OlapModel, config: DatabricksConnectionConfig) -> None:
        self._model = model
        self._config = config

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
        join_sql = []

        # Determine which dimension attributes are needed
        requested_cols = set()
        if request:
            requested_cols.update(request.rows)
            requested_cols.update(request.columns)

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

            # Only join dimension if it has attributes to include
            if not dim_attrs_to_include and not dim_key_include:
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

        # Include only requested metrics (or all if request is None)
        requested_metrics = set(request.metrics) if request else set()
        for metric in self._model.metrics:
            if request is None or not requested_metrics or metric.name in requested_metrics:
                select_cols.append(
                    f"SUM({fact_alias}.{self._q(metric.name)}) AS {self._q(metric.name)}"
                )

        group_by_cols.append("'1'")  # dummy group by to allow aggregation without dimensions

        return "\n".join(
            [
                "SELECT",
                "  " + ",\n  ".join(select_cols),
                f"FROM {self._qualified_table_name(self._model.fact)} {fact_alias}",
                *join_sql,
                "GROUP BY",
                "  " + ",\n  ".join(group_by_cols),
            ]
        )

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
        self._sql_builder = OlapSqlBuilder(model, self._config)
        self._flat: pd.DataFrame | None = None  # lazy cache
        self._spark = self._get_spark_session()
        LOGGER.info("Initialized Spark backend for model=%s", self._model.name)

    # -- Public interface ----------------------------------------------------

    def flat_table(self) -> pd.DataFrame:
        raise ValueError("flat_table() is deprecated!!!")
        """
        Return the fully joined flat table with synthetic display keys added.
        Result is cached — Spark join is executed once per process.
        """
        if self._flat is None:
            LOGGER.info("Building Spark flat table for model=%s", self._model.name)
            self._flat = self._build_flat_table()
            LOGGER.info("Spark flat table built: shape=%s", self._flat.shape)
        return self._flat

    def execute(self, request: OlapQueryRequest) -> pd.DataFrame:
        """
        Execute OLAP query via Spark SQL.

        Queries the backend fresh for each execution (no caching of aggregated results).
        This ensures row counts update correctly as dimensions are added/removed
        during drill-down interactions.
        """
        LOGGER.debug(
            "Spark execute: rows=%s columns=%s metrics=%s filters=%s",
            request.rows,
            request.columns,
            request.metrics,
            list((request.filters or {}).keys()),
        )
        # Build and execute SQL via Spark
        sql = self._sql_builder.build_sql(request)
        LOGGER.debug("Executing Spark SQL:\n%s", sql)
        df = self._spark.sql(sql).toPandas()

        # Apply filters post-query
        for attr, allowed in (request.filters or {}).items():
            if attr in df.columns and allowed:
                df = df[df[attr].isin(allowed)]

        LOGGER.debug("Spark execute result shape=%s", df.shape)
        return df

    # -- Private helpers -----------------------------------------------------

    def _build_flat_table(self) -> pd.DataFrame:
        raise ValueError("flat_table() is deprecated!!!")
        """Join fact + dimensions from Databricks tables and return pandas DataFrame."""

        fact_name = self._qualified_table_name(self._model.fact)
        LOGGER.info("Reading fact table from Spark: %s", fact_name)
        fact = self._spark.table(fact_name)

        df = fact
        for dim in self._model.dimensions:
            dim_name = self._sql_builder._qualified_table_name(dim)
            LOGGER.info("Joining dimension table from Spark: %s", dim_name)
            dim_cols = [dim.dim_key] + [a.name for a in dim.attributes]
            dim_df = self._spark.table(dim_name).select(*dim_cols)

            dim_join_key = f"__{dim.name.lower().replace(' ', '_')}_dim_key"
            dim_df = dim_df.withColumnRenamed(dim.dim_key, dim_join_key)

            df = df.join(dim_df, on=(df[dim.fact_key] == dim_df[dim_join_key]), how="left")
            df = df.drop(dim_join_key)

            # Add synthetic display key alias (copy of fact foreign key)
            df = df.withColumn(dim.display_key, df[dim.fact_key])

        return df.toPandas()

    @staticmethod
    def _get_spark_session():

        """
        try:
            spark_mod = importlib.import_module("pyspark.sql")
            SparkSession = getattr(spark_mod, "SparkSession")
        except Exception as ex:
            raise RuntimeError(
                "pyspark is not available. This backend must run inside Databricks workspace."
            ) from ex """
        
        #spark = DatabricksSession.builder.getOrCreate()        
        spark = DatabricksSession.builder.serverless().getOrCreate()

        #spark = SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()
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
        self._sql_builder = OlapSqlBuilder(model, self._config)
        self._flat: pd.DataFrame | None = None
        self._validate_config()
        LOGGER.info("Initialized SQL backend for model=%s", self._model.name)

    def execute(self, request: OlapQueryRequest) -> pd.DataFrame:
        """
        Execute OLAP query via SQL connector.
        
        Query backend fresh for each execution (bypasses flat_table cache).
        This ensures row counts update correctly as dimensions are added/removed
        during drill-down interactions.
        """
        LOGGER.debug(
            "SQL execute: rows=%s columns=%s metrics=%s filters=%s",
            request.rows,
            request.columns,
            request.metrics,
            list((request.filters or {}).keys()),
        )
        # Build SQL query using shared builder
        sql = self._sql_builder.build_sql(request)
        LOGGER.debug("Executing SQL query:\n%s", sql)
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            df = pd.DataFrame(rows, columns=cols)
        except Exception as ex:
            LOGGER.error("SQL query failed:\n%s", sql, exc_info=True)
            raise RuntimeError("Failed to execute SQL query. See logs for details.") from ex
        finally:
            conn.close()

        # Apply filters post-query
        for attr, allowed in (request.filters or {}).items():
            if attr in df.columns and allowed:
                df = df[df[attr].isin(allowed)]

        LOGGER.debug("SQL execute result shape=%s", df.shape)
        return df

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

        LOGGER.info(
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


class DatabricksBackend(DatabricksSparkBackend):
    """Backward-compatible alias to Spark backend."""

    pass


class CsvBackend(DatabricksSparkBackend):
    """
    Backward-compatible alias used by app.py.

    Despite the name, this implementation now reads from Databricks Spark tables.
    """

    pass
