"""
src/model.py
------------
Loads and parses model.yaml into typed dataclasses.

The rest of the application imports OlapModel and uses its properties to
discover dimensions, metrics, and join relationships — nothing is hard-coded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml 


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class AttributeDef:
    """A single dimension attribute column."""
    name: str
    label: str
    type: Literal["string", "integer", "numeric", "date"]
    default_show: bool = False  # Ensure this is parsed from YAML


@dataclass
class MetricDef:
    """A numeric measure on the fact table."""
    name: str
    label: str
    type: Literal["numeric", "integer"]
    default_agg: str
    allowed_aggs: list[str]
    expr: str | None = None
    default_show: bool = False  # Ensure this is parsed from YAML


@dataclass
class DimensionDef:
    """One dimension table and its relationship to the fact table."""
    name: str           # Human-readable name, e.g. "Date"
    catalog: str | None # Optional catalog (Databricks)
    schema: str | None  # Optional schema (Databricks)
    table: str          # Physical table name
    fact_key: str       # FK column on the fact table
    dim_key: str        # PK column on the dimension table
    display_key: str    # Synthetic alias shown in the UI (Key column)
    default_show: bool = False  # Ensure this is parsed from YAML
    attributes: list[AttributeDef] = field(default_factory=list)

    @property
    def all_columns(self) -> list[str]:
        """display_key first, then each attribute name."""
        return [self.display_key] + [a.name for a in self.attributes]


@dataclass
class FactDef:
    """The fact table definition."""
    name: str
    catalog: str | None
    schema: str | None
    table: str
    join_keys: list[str]    # Surrogate keys used for joins only (hidden in UI)
    metrics: list[MetricDef] = field(default_factory=list)


# ---------------------------------------------------------------------------
# OlapModel
# ---------------------------------------------------------------------------

class OlapModel:
    """
    Parsed representation of model.yaml.

    Usage
    -----
    model = OlapModel.from_yaml(Path("model.yaml"))
    model.fact            -> FactDef
    model.dimensions      -> list[DimensionDef]
    model.metrics         -> list[MetricDef]
    model.dimension("Date") -> DimensionDef
    """

    def __init__(self, name: str, fact: FactDef, dimensions: list[DimensionDef]) -> None:
        self._name = name
        self._fact = fact
        self._dimensions = dimensions
        self._dim_by_name: dict[str, DimensionDef] = {d.name: d for d in dimensions}

    # -- Construction --------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: Path) -> "OlapModel":
        with open(path, "r") as f:
            raw = yaml.safe_load(f)
        return cls._parse(raw, default_name=path.stem)

    @classmethod
    def from_dict(cls, raw: dict, default_name: str = "model") -> "OlapModel":
        return cls._parse(raw, default_name=default_name)

    @classmethod
    def _parse(cls, raw: dict, default_name: str) -> "OlapModel":
        model_name = raw.get("model_name") or raw.get("name") or default_name

        # Fact
        rf = raw["fact"]
        fact = FactDef(
            name=rf["name"],
            catalog=rf.get("catalog"),
            schema=rf.get("schema"),
            table=rf["table"],
            join_keys=rf.get("join_keys", []),
            metrics=[
                MetricDef(
                    name=m["name"],
                    label=m.get("label", m["name"]),
                    type=m.get("type", "numeric"),
                    default_agg=m.get("default_agg", "sum"),
                    allowed_aggs=m.get("allowed_aggs", ["sum"]),
                    expr=m.get("expr"),
                        default_show=bool(m.get("default_show", False)),  # Parse from YAML
                )
                for m in rf.get("metrics", [])
            ],
        )

        # Dimensions
        dimensions = [
            DimensionDef(
                name=d["name"],
                catalog=d.get("catalog"),
                schema=d.get("schema"),
                table=d["table"],
                fact_key=d["fact_key"],
                dim_key=d["dim_key"],
                display_key=d["display_key"],
                    default_show=bool(d.get("default_show", False)),  # Parse from YAML
                attributes=[
                    AttributeDef(
                        name=a["name"],
                        label=a.get("label", a["name"]),
                        type=a.get("type", "string"),
                            default_show=bool(a.get("default_show", False)),  # Parse from YAML
                    )
                    for a in d.get("attributes", [])
                ],
            )
            for d in raw.get("dimensions", [])
        ]

        return cls(name=model_name, fact=fact, dimensions=dimensions)

    # -- Accessors -----------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def fact(self) -> FactDef:
        return self._fact

    @property
    def dimensions(self) -> list[DimensionDef]:
        return self._dimensions

    @property
    def metrics(self) -> list[MetricDef]:
        return self._fact.metrics

    def dimension(self, name: str) -> DimensionDef:
        return self._dim_by_name[name]

    @property
    def all_join_keys(self) -> set[str]:
        """All surrogate key columns — should be hidden in the UI."""
        keys: set[str] = set(self._fact.join_keys)
        for d in self._dimensions:
            keys.add(d.dim_key)
        return keys

    @property
    def display_keys(self) -> set[str]:
        """Synthetic display-key aliases for each dimension."""
        return {d.display_key for d in self._dimensions}

    @property
    def metric_names(self) -> set[str]:
        return {m.name for m in self._fact.metrics}


# ---------------------------------------------------------------------------
# MetricViewDef  –  direct representation of a Databricks metric view
# ---------------------------------------------------------------------------

@dataclass
class MvField:
    """One selectable field exposed by a Databricks metric view."""
    name: str               # column name on the metric view
    label: str              # human-readable label
    field_type: str         # "dimension_key" | "dimension_attr" | "measure"
    group_name: str         # UI panel group: dimension display name, or "Metrics"
    default_show: bool = True


@dataclass
class MetricViewDef:
    """
    Parsed representation of a Databricks metric view.

    Replaces the OlapModel YAML round-trip for metric-view-backed models.
    All fields are directly queryable from the metric view object; no joins
    to underlying dimension tables are needed for the main query path.
    """
    model_id: str
    catalog: str
    schema: str
    metric_view_name: str   # physical object name — used in FROM clause
    display_name: str       # human-readable name shown in the UI

    # Ordered list of all selectable fields (dimensions then measures)
    fields: list[MvField]

    @property
    def dimension_fields(self) -> list[MvField]:
        return [f for f in self.fields if f.field_type in ("dimension_key", "dimension_attr")]

    @property
    def measures(self) -> list[MvField]:
        return [f for f in self.fields if f.field_type == "measure"]

    @property
    def all_field_names(self) -> set[str]:
        return {f.name for f in self.fields}

    @property
    def groups(self) -> list[str]:
        """Ordered unique dimension group names (excludes 'Metrics')."""
        seen: list[str] = []
        for f in self.fields:
            if f.group_name != "Metrics" and f.group_name not in seen:
                seen.append(f.group_name)
        return seen

    def fields_for_group(self, group_name: str) -> list[MvField]:
        return [f for f in self.fields if f.group_name == group_name]


# ---------------------------------------------------------------------------
# Serialization helpers for dcc.Store transport
# ---------------------------------------------------------------------------

def metric_view_def_to_dict(mvd: "MetricViewDef") -> dict:
    """Serialize a MetricViewDef to a JSON-safe dict for storage in dcc.Store."""
    return {
        "model_id": mvd.model_id,
        "catalog": mvd.catalog,
        "schema": mvd.schema,
        "metric_view_name": mvd.metric_view_name,
        "display_name": mvd.display_name,
        "fields": [
            {
                "name": f.name,
                "label": f.label,
                "field_type": f.field_type,
                "group_name": f.group_name,
                "default_show": f.default_show,
            }
            for f in mvd.fields
        ],
    }


def metric_view_def_from_dict(d: dict) -> "MetricViewDef":
    """Deserialize a MetricViewDef from a dcc.Store dict."""
    return MetricViewDef(
        model_id=d["model_id"],
        catalog=d["catalog"],
        schema=d["schema"],
        metric_view_name=d["metric_view_name"],
        display_name=d["display_name"],
        fields=[
            MvField(
                name=f["name"],
                label=f["label"],
                field_type=f["field_type"],
                group_name=f["group_name"],
                default_show=f.get("default_show", True),
            )
            for f in d.get("fields", [])
        ],
    )
