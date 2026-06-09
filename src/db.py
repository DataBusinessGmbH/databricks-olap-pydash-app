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
from flask import request as flask_request
import pandas as pd

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

    server_hostname: str | None = None
    http_path: str | None = None
    access_token: str | None = None
    default_catalog: str | None = None
    default_schema: str | None = None


def load_databricks_config_from_env() -> DatabricksConnectionConfig:

    """Load Databricks backend config from environment variables."""
    raw_host = (os.getenv("DATABRICKS_HOST") or "").strip()
    if raw_host.startswith("https://"):
        raw_host = raw_host[len("https://"):]
    elif raw_host.startswith("http://"):
        raw_host = raw_host[len("http://"):]
    raw_host = raw_host.rstrip("/")

    cfg = DatabricksConnectionConfig(
        server_hostname=raw_host or None,
        http_path=(os.getenv("DATABRICKS_HTTP_PATH") or "").strip() or None,
        access_token=(os.getenv("DATABRICKS_TOKEN") or "").strip() or None,
        default_catalog=(os.getenv("DATABRICKS_DEFAULT_CATALOG") or "").strip() or None,
        default_schema=(os.getenv("DATABRICKS_DEFAULT_SCHEMA") or "").strip() or None,
    )
    LOGGER.info(
        "Databricks config loaded: host=%s, http_path=%s, token=%s, catalog=%s, schema=%s",
        cfg.server_hostname,
        cfg.http_path,
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
# Protocol  (the interface any backend must satisfy)
# ---------------------------------------------------------------------------
class OlapDatabase(Protocol):
    """Any backend that can execute SQL and return pandas DataFrames."""

    def current_user(self) -> str:
        """Return the current backend user, or a safe fallback."""
        ...

    def execute_sql(self, sql: str) -> pd.DataFrame:
        """Execute raw SQL and return result as DataFrame."""
        ...

class DatabricksSqlBackend:
    """
    Databricks SQL Warehouse backend using `databricks-sql-connector`.
    """
    def __init__(self, config: DatabricksConnectionConfig | None = None) -> None:
        self._config = config or load_databricks_config_from_env()
        self._validate_config()
        LOGGER.info("Initialized SQL backend")

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
        try:
            user_df = self.execute_sql("SELECT current_user() AS current_user")
            if not user_df.empty and "current_user" in user_df.columns:
                return str(user_df.iloc[0]["current_user"])
        except Exception:
            LOGGER.warning("Failed to resolve SQL current user", exc_info=True)
        return "Unknown user"

    def _validate_config(self) -> None:
        missing = []
        if not self._config.server_hostname:
            missing.append("DATABRICKS_HOST")
        if not self._config.http_path:
            missing.append("DATABRICKS_HTTP_PATH")
        if missing:
            LOGGER.error("SQL backend config missing env vars: %s", ", ".join(missing))
            raise RuntimeError(
                "Databricks SQL backend missing required env vars: " + ", ".join(missing)
            )

    @staticmethod
    def _token_from_request_header() -> str:
        token = ""
        try:
            token = (flask_request.headers.get("x-forwarded-access-token") or "").strip()
        except RuntimeError:
            # No active request context; fallback to env token below.
            token = ""

        if not token:
            token = (os.getenv("DATABRICKS_TOKEN") or "").strip()

        if not token:
            raise RuntimeError(
                "Missing Databricks access token: provide x-forwarded-access-token header or DATABRICKS_TOKEN env var."
            )
        return token

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
        access_token = self._token_from_request_header()
        connect_kwargs = {
            "server_hostname": self._config.server_hostname,
            "http_path": self._config.http_path,
            "access_token": access_token,
        }
        if self._config.default_catalog:
            connect_kwargs["catalog"] = self._config.default_catalog
        if self._config.default_schema:
            connect_kwargs["schema"] = self._config.default_schema
        return sql_mod.connect(**connect_kwargs)


def create_databricks_backend(
    config: DatabricksConnectionConfig | None = None
) -> OlapDatabase:
    """
    Create the Databricks SQL backend executor.
    """
    cfg = config or load_databricks_config_from_env()
    LOGGER.info("Creating Databricks SQL backend")
    return DatabricksSqlBackend(cfg)

