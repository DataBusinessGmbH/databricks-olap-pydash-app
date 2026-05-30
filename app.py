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

import dash_ag_grid as dag
import pandas as pd
from dash import Dash, Input, Output, State, dcc, html, no_update
import dash

from src.model import OlapModel, DimensionDef, MetricDef
from src.db import LOGGER, OlapDatabase, OlapQueryRequest, create_databricks_backend
import logging

# ---------------------------------------------------------------------------
# Bootstrap: load model, connect database layer
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
MODELS_DIR = BASE_DIR / "models"
DEFAULT_MAX_ROWS = 1000


def load_env_on_startup() -> None:
    """
    Load environment variables from .env automatically.

    - If .env exists, load it without overriding already-exported environment vars.
    - If .env does not exist, default backend mode to spark.
    """
    env_path = BASE_DIR / ".env"

    if env_path.exists():
        try:
            dotenv = __import__("dotenv")
            dotenv.load_dotenv(dotenv_path=env_path, override=False)
        except Exception:
            # If python-dotenv is unavailable, continue with existing process env.
            pass

    os.environ.setdefault("DATABRICKS_BACKEND_MODE", "spark")


load_env_on_startup()


def discover_model_files() -> dict[str, Path]:
    """Return {model_id: model_yaml_path} for all available model YAML files."""
    model_files: dict[str, Path] = {}

    if MODELS_DIR.exists():
        for p in sorted(MODELS_DIR.glob("*.y*ml")):
            model_files[p.stem] = p

    if not model_files:
        raise FileNotFoundError(
            "No model YAML found. Add one under models/*.yaml."
        )
    
    LOGGER.info(f"Discovered model files: {model_files}")

    return model_files


MODEL_FILES = discover_model_files()
DEFAULT_MODEL_ID = next(iter(MODEL_FILES.keys()))
MODEL_CACHE: dict[str, OlapModel] = {}
DB_CACHE: dict[str, OlapDatabase] = {}


def get_model(model_id: str) -> OlapModel:
    if model_id not in MODEL_CACHE:
        MODEL_CACHE[model_id] = OlapModel.from_yaml(MODEL_FILES[model_id])
    return MODEL_CACHE[model_id]


def get_backend(model_id: str) -> OlapDatabase:
    if model_id not in DB_CACHE:
        DB_CACHE[model_id] = create_databricks_backend(get_model(model_id))
    return DB_CACHE[model_id]


def get_flat_table(model_id: str) -> pd.DataFrame:
    request = OlapQueryRequest(max_rows=DEFAULT_MAX_ROWS)
    return get_backend(model_id).execute(request)


def sanitize_max_rows(value) -> int | None:
    if value in (None, ""):
        return DEFAULT_MAX_ROWS
    try:
        max_rows = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ROWS
    return max_rows if max_rows > 0 else DEFAULT_MAX_ROWS


def get_logged_in_user(model_id: str) -> str:
    return get_backend(model_id).current_user()


def get_filter_values_map(model_id: str, max_values: int = 500) -> dict[str, list]:
    model = get_model(model_id)
    backend = get_backend(model_id)
    values_map: dict[str, list] = {}

    for dim in model.dimensions:
        fields = [dim.dim_key] + [a.name for a in dim.attributes]
        for field_name in fields:
            try:
                values_map[field_name] = backend.filter_values(field_name, max_values=max_values)
            except Exception:
                LOGGER.warning(
                    "Failed to fetch filter values for model=%s field=%s",
                    model_id,
                    field_name,
                    exc_info=True,
                )
                values_map[field_name] = []

    return values_map


def build_request_from_grid_state(column_state, max_rows_value) -> OlapQueryRequest:
    max_rows = sanitize_max_rows(max_rows_value)

    if not column_state:
        return OlapQueryRequest(max_rows=max_rows)

    row_groups: list[str] = []
    pivot_cols: list[str] = []

    for col_state in column_state:
        if col_state.get("hide"):
            continue
        if col_state.get("rowGroup"):
            row_groups.append(col_state["colId"])
            continue
        if col_state.get("pivot"):
            pivot_cols.append(col_state["colId"])
            continue
        row_groups.append(col_state["colId"])

    return OlapQueryRequest(
        rows=row_groups,
        columns=pivot_cols,
        metrics=[],
        filters={},
        max_rows=max_rows,
    )


def build_filters_from_filter_model(filter_model) -> dict[str, list]:
    filters: dict[str, list] = {}
    if not filter_model:
        return filters

    for field_name, cfg in filter_model.items():
        if not isinstance(cfg, dict):
            continue

        values = cfg.get("values")
        if isinstance(values, list) and values:
            filters[field_name] = values
            continue

        single = cfg.get("filter")
        if single not in (None, ""):
            filters[field_name] = [single]

    return filters


def get_model_dropdown_options() -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for model_id in MODEL_FILES.keys():
        label = get_model(model_id).name
        options.append({"label": label, "value": model_id})
    return options

# ---------------------------------------------------------------------------
# Grid column-definition builders  (driven entirely by the model)
# ---------------------------------------------------------------------------

def _label(name: str) -> str:
    return name.replace("_", " ").title()


def _dim_field_def(col_name: str, dim: DimensionDef, filter_values: list | None = None) -> dict:
    """Build an AG Grid column def for a dimension attribute or display key."""
    is_display_key = col_name == dim.display_key

    label = "Key" if is_display_key else next(
        (a.label for a in dim.attributes if a.name == col_name),
        _label(col_name),
    )

    col_def = {
        "field": col_name,
        "headerName": label,
        "sortable": True,
        "filter": "agSetColumnFilter",
        "resizable": True,
        "hide": is_display_key,
        "enablePivot": True,
        "enableRowGroup": True,
    }

    if filter_values is not None:
        col_def["filterParams"] = {"values": filter_values}

    return col_def


def _metric_field_def(metric: MetricDef) -> dict:
    """Build an AG Grid column def for a metric."""
    return {
        "field": metric.name,
        "headerName": metric.label,
        "sortable": True,
        "filter": True,
        "resizable": True,
        "type": "numericColumn",
        "enableValue": True,
        "aggFunc": metric.default_agg,
        "allowedAggFuncs": metric.allowed_aggs,
    }


def _join_key_field_def(key: str) -> dict:
    """Surrogate join keys — always hidden, never grouped/pivoted."""
    return {
        "field": key,
        "headerName": _label(key),
        "hide": True,
        "sortable": False,
        "filter": False,
        "resizable": False,
    }


def build_column_defs(
    df: pd.DataFrame,
    model: OlapModel,
    filter_values_map: dict[str, list] | None = None,
) -> list[dict]:
    """
    Build the full grouped column-def tree for AG Grid.
    Groups: one per dimension + Metrics. Surrogate keys are hidden leaves.
    """
    defs: list[dict] = []
    known_fields: set[str] = set()
    values_map = filter_values_map or {}

    # Dimension groups
    for dim in model.dimensions:
        children: list[dict] = []
        if dim.display_key in df.columns or 1==1:
            children.append(_dim_field_def(dim.dim_key, dim, values_map.get(dim.dim_key)))
            known_fields.add(dim.display_key)
        for attr in dim.attributes:
            if attr.name in df.columns or 1==1:
                children.append(_dim_field_def(attr.name, dim, values_map.get(attr.name)))
                known_fields.add(attr.name)
        if children:
            defs.append({"headerName": dim.name, "children": children})

    # Metrics group
    metric_children = [_metric_field_def(m) for m in model.metrics if m.name in df.columns]
    for child in metric_children:
        known_fields.add(child["field"])
    if metric_children:
        defs.append({"headerName": "Metrics", "children": metric_children})

    # Hidden surrogate join keys
    for key in model.all_join_keys:
        if key in df.columns and key not in known_fields:
            defs.append(_join_key_field_def(key))
            known_fields.add(key)

    return defs


def build_grid_options() -> dict:
    return {
        "animateRows": True,
        "rowGroupPanelShow": "always",
        "pivotPanelShow": "always",
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
                    "toolPanelParams": {"suppressRowGroups": False},
                },
                {
                    "id": "filters",
                    "labelDefault": "Filters",
                    "labelKey": "filters",
                    "iconKey": "filter",
                    "toolPanel": "agFiltersToolPanel",
                },
            ],
            "defaultToolPanel": "columns",
        },
    }


# ---------------------------------------------------------------------------
# Dash layout
# ---------------------------------------------------------------------------

app = Dash(__name__)

initial_model = get_model(DEFAULT_MODEL_ID)
initial_df = get_flat_table(DEFAULT_MODEL_ID)
initial_filter_values_map = get_filter_values_map(DEFAULT_MODEL_ID)
initial_user = get_logged_in_user(DEFAULT_MODEL_ID)

app.layout = html.Div(
    style={
        "fontFamily": "Arial, sans-serif",
        "padding": "16px",
        "width": "80vw",
        "maxWidth": "80vw",
        "margin": "0 auto",
    },
    children=[
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
                    style={"maxWidth": "320px", "minWidth": "240px"},
                    children=[
                        html.Div("Model", style={"fontWeight": "600", "marginBottom": "6px"}),
                        dcc.Dropdown(
                            id="model-selector",
                            options=get_model_dropdown_options(),
                            value=DEFAULT_MODEL_ID,
                            clearable=False,
                        ),
                    ],
                ),
                html.Div(
                    style={"maxWidth": "180px"},
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
            ],
        ),
        html.Div(
            "Use the right-side collapsible panel to switch between Columns and Filters.",
            style={"color": "#374151", "marginBottom": "12px"},
        ),
        html.Div(
            style={
                "border": "1px solid #d1d5db",
                "borderRadius": "8px",
                "padding": "10px",
                "background": "#fff",
            },
            children=[
                dag.AgGrid(
                    id="olap-grid",
                    rowData=initial_df.to_dict("records"),
                    columnDefs=build_column_defs(initial_df, initial_model, initial_filter_values_map),
                    defaultColDef={
                        "flex": 1,
                        "minWidth": 120,
                        "filter": True,
                        "floatingFilter": True,
                    },
                    enableEnterpriseModules=True,
                    className="ag-theme-alpine",
                    dashGridOptions=build_grid_options(),
                    style={"height": "78vh", "width": "100%"},
                )
            ],
        ),
    ],
)


@app.callback(
    Output("olap-grid", "rowData"),
    Output("olap-grid", "columnDefs"),
    Output("logged-in-user", "children"),
    Input("model-selector", "value"),
    Input("max-rows-input", "value"),
    State("olap-grid", "columnState"),
    State("olap-grid", "filterModel"),
)
def on_model_change(model_id: str, max_rows_value, column_state, filter_model):
    selected_model = get_model(model_id)
    request = build_request_from_grid_state(column_state, max_rows_value)
    request.filters = build_filters_from_filter_model(filter_model)
    selected_df = get_backend(model_id).execute(request)
    selected_filter_values_map = get_filter_values_map(model_id)
    return (
        selected_df.to_dict("records"),
        build_column_defs(selected_df, selected_model, selected_filter_values_map),
        get_logged_in_user(model_id),
    )


@app.callback(
    Output("olap-grid", "rowData", allow_duplicate=True),
    Input("olap-grid", "columnState"),
    Input("olap-grid", "filterModel"),
    Input("model-selector", "value"),
    Input("max-rows-input", "value"),
    prevent_initial_call=True,
)
def on_grid_dimension_change(column_state, filter_model, model_id: str, max_rows_value):
    """
    Triggered when user drags/drops dimensions to row group or pivot areas.
    
    Extracts row groups and pivot columns from AG Grid column state,
    then queries the backend with the new dimensions to ensure accurate
    aggregations based on the current drill-down configuration.
    """
    logger = logging.getLogger(__name__)

    # Then in the placeholder:
    logger.info(f"Dimensions changed for model {model_id}")    
    #logger.info(f"Column state: {column_state}")

    request = build_request_from_grid_state(column_state, max_rows_value)
    request.filters = build_filters_from_filter_model(filter_model)
    logger.info(f"Row groups: {request.rows}")
    logger.info(f"Pivot columns: {request.columns}")
    logger.info(f"Filter fields: {list((request.filters or {}).keys())}")
    
    backend = get_backend(model_id)
    result_df = backend.execute(request)
    
    return result_df.to_dict("records")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8050)
