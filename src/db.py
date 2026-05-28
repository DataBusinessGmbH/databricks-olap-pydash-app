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

The OlapDatabase interface is intentionally abstract so that the CSV
backend (CsvBackend) can be swapped for a SQL / Databricks / Snowflake
backend without touching app.py.

Public API
----------
  request = OlapQueryRequest(
      rows=["year", "quarter"],
      columns=["category"],
      metrics=["sales_amount"],
      filters={"region": ["North", "South"]},
  )
  db = CsvBackend(model, base_dir=Path("."))
  df: pd.DataFrame = db.execute(request)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import pandas as pd

from src.model import OlapModel


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


# ---------------------------------------------------------------------------
# CSV Backend  (current implementation — backed by local CSV files)
# ---------------------------------------------------------------------------

class CsvBackend:
    """
    Implements OlapDatabase using the CSV files referenced in model.yaml.

    To switch to a real database, implement the same two methods on a new
    class (e.g. DatabricksBackend) and pass it to the app instead.
    """

    def __init__(self, model: OlapModel, base_dir: Path) -> None:
        self._model = model
        self._base_dir = base_dir
        self._flat: pd.DataFrame | None = None  # lazy cache

    # -- Public interface ----------------------------------------------------

    def flat_table(self) -> pd.DataFrame:
        """
        Return the fully joined flat table with synthetic display keys added.
        Result is cached — CSV files are only read once per process.
        """
        if self._flat is None:
            self._flat = self._build_flat_table()
        return self._flat

    def execute(self, request: OlapQueryRequest) -> pd.DataFrame:
        """
        Execute a generic OLAP query and return a DataFrame.

        Behaviour
        ---------
        * If rows and columns are both empty  → return the flat table
          (optionally filtered and metric-projected).
        * If rows is non-empty and columns is empty → GROUP BY rows,
          aggregate metrics.
        * If both rows and columns are non-empty → GROUP BY rows,
          aggregate metrics, then pivot on columns.
        * filters are applied before aggregation.
        """
        df = self.flat_table().copy()

        # 1. Apply filters
        for attr, allowed in (request.filters or {}).items():
            if attr in df.columns and allowed:
                df = df[df[attr].isin(allowed)]

        # 2. Resolve metrics
        all_metric_names = [m.name for m in self._model.metrics]
        metrics = request.metrics if request.metrics else all_metric_names
        metrics = [m for m in metrics if m in df.columns]

        # 3. No grouping → return full flat table (all columns, already filtered)
        if not request.rows and not request.columns:
            return df

        # 4. Group-by aggregation
        group_cols = list(dict.fromkeys(request.rows + request.columns))
        group_cols = [c for c in group_cols if c in df.columns]

        agg_map = {
            m.name: m.default_agg
            for m in self._model.metrics
            if m.name in metrics
        }
        # pandas named-agg via dict: {metric: (metric, agg_func)}
        agg_kwargs = {m: (m, func) for m, func in agg_map.items()}
        df = df.groupby(group_cols, as_index=False).agg(**agg_kwargs)

        # 5. Pivot (optional)
        if request.rows and request.columns:
            df = df.pivot_table(
                index=request.rows,
                columns=request.columns,
                values=metrics,
                aggfunc="sum",
            ).reset_index()
            # Flatten multi-level column names
            df.columns = [
                "_".join(str(part) for part in col).strip("_") if isinstance(col, tuple) else col
                for col in df.columns
            ]

        return df

    # -- Private helpers -----------------------------------------------------

    def _build_flat_table(self) -> pd.DataFrame:
        """Join fact + all dimension tables and add synthetic display keys."""
        fact_path = self._base_dir / self._model.fact.source
        df = pd.read_csv(fact_path)

        for dim in self._model.dimensions:
            dim_path = self._base_dir / dim.source
            dim_df = pd.read_csv(dim_path)
            df = df.merge(dim_df, left_on=dim.fact_key, right_on=dim.dim_key, how="left")
            # Add synthetic display-key alias (copy of the join key)
            df[dim.display_key] = df[dim.fact_key]

        return df
