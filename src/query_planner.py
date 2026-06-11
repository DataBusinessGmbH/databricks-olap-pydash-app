"""
query_planner.py
----------------
Model-aware SQL planning helpers for MetricViewDef-backed queries.

This module is intentionally separate from the Databricks backend executor.
It only builds SQL and delegates execution to a provided backend.
"""

from __future__ import annotations

import pandas as pd

from src.db import OlapDatabase, OlapQueryRequest
from src.model import MetricViewDef


def _quoted_ident(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _sql_literal(value) -> str | None:
    if value is None:
        return None
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def _in_filter_sql(expr: str, allowed_values: list) -> str | None:
    literals = [_sql_literal(v) for v in (allowed_values or [])]
    literals = [v for v in literals if v is not None]
    if not literals:
        return None
    return f"CAST(TRIM({expr}) AS STRING) IN ({', '.join(literals)})"


def _not_in_filter_sql(expr: str, excluded_values: list) -> str | None:
    literals = [_sql_literal(v) for v in (excluded_values or [])]
    literals = [v for v in literals if v is not None]
    if not literals:
        return None
    return f"(COALESCE(CAST(TRIM({expr}) AS STRING), '') NOT IN ({', '.join(literals)}))"


def _normalize_filter_spec(spec) -> dict[str, list]:
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


def _filter_sql(expr: str, spec) -> str | None:
    normalized = _normalize_filter_spec(spec)
    parts: list[str] = []

    in_sql = _in_filter_sql(expr, normalized.get("in", []))
    if in_sql:
        parts.append(in_sql)

    not_in_sql = _not_in_filter_sql(expr, normalized.get("not_in", []))
    if not_in_sql:
        parts.append(not_in_sql)

    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return "(" + " AND ".join(parts) + ")"


def _qualified_mv_name(mv_def: MetricViewDef) -> str:
    parts = [p for p in [mv_def.catalog, mv_def.schema, mv_def.metric_view_name] if p]
    if not parts:
        raise RuntimeError("MetricViewDef has no table name.")
    return ".".join(_quoted_ident(p) for p in parts)


def build_sql_for_request(mv_def: MetricViewDef, request: OlapQueryRequest | None = None) -> str:
    fact_alias = "f"
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

    for f in mv_def.dimension_fields:
        if include_all or f.name in requested_rows:
            expr = f"{fact_alias}.{_quoted_ident(f.name)}"
            select_cols.append(f"{expr} AS {_quoted_ident(f.name)}")
            group_by_cols.append(expr)
        if request and request.filters and f.name in request.filters:
            where_sql = _filter_sql(f"{fact_alias}.{_quoted_ident(f.name)}", request.filters[f.name])
            if where_sql:
                where_clauses.append(where_sql)

    for f in mv_def.measures:
        if include_all or not requested_metrics or f.name in requested_metrics:
            select_cols.append(f"MEASURE({_quoted_ident(f.name)}) AS {_quoted_ident(f.name)}")
        if request and request.filters and f.name in request.filters:
            where_sql = _filter_sql(f"{fact_alias}.{_quoted_ident(f.name)}", request.filters[f.name])
            if where_sql:
                where_clauses.append(where_sql)

    sql_parts = [
        "SELECT",
        "  " + ",\n  ".join(select_cols),
        f"FROM {_qualified_mv_name(mv_def)} {fact_alias}",
    ]

    if where_clauses:
        unique_clauses = list(dict.fromkeys(where_clauses))
        sql_parts += ["WHERE", "  " + "\n  AND ".join(unique_clauses)]

    if group_by_cols:
        sql_parts += ["GROUP BY", "  " + ",\n  ".join(group_by_cols)]

    max_rows = request.max_rows if request else None
    if isinstance(max_rows, int) and max_rows > 0:
        sql_parts.append(f"LIMIT {max_rows}")

    return "\n".join(sql_parts)


def build_filter_values_sql(mv_def: MetricViewDef, field_name: str, max_values: int = 500) -> str:
    if field_name not in mv_def.all_field_names:
        raise ValueError(f"Unknown field for filter values: {field_name}")
    fact_alias = "f"
    max_vals = max_values if isinstance(max_values, int) and max_values > 0 else 500
    expr = f"{fact_alias}.{_quoted_ident(field_name)}"
    return "\n".join([
        "SELECT DISTINCT",
        f"  {expr} AS {_quoted_ident(field_name)}",
        f"FROM {_qualified_mv_name(mv_def)} {fact_alias}",
        f"WHERE {expr} IS NOT NULL",
        "ORDER BY 1",
        f"LIMIT {max_vals}",
    ])


def execute_olap_request(backend: OlapDatabase, mv_def: MetricViewDef, request: OlapQueryRequest) -> pd.DataFrame:
    sql = build_sql_for_request(mv_def, request)
    return backend.execute_sql(sql)


def fetch_filter_values(
    backend: OlapDatabase,
    mv_def: MetricViewDef,
    field_name: str,
    max_values: int = 500,
) -> list:
    sql = build_filter_values_sql(mv_def, field_name, max_values=max_values)
    values_df = backend.execute_sql(sql)
    if values_df.empty:
        return []
    return [v for v in values_df.iloc[:, 0].tolist() if pd.notna(v)]
