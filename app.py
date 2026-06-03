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
from flask import jsonify, request as flask_request

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

    LOGGER.info(f"Parsed grid state into request: row_fields={row_fields}, col_fields={col_fields}, max_rows={max_rows}")

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
    options: list[dict[str, str]] = []
    for model_id in MODEL_FILES.keys():
        label = get_model(model_id).name
        options.append({"label": label, "value": model_id})
    return options


def get_allowed_filter_fields(model: OlapModel) -> set[str]:
    fields: set[str] = set(model.fact.join_keys)
    for dim in model.dimensions:
        fields.add(dim.dim_key)
        fields.add(dim.display_key)
        for attr in dim.attributes:
            fields.add(attr.name)
    return fields


def parse_manual_filters(filter_text: str | None, model: OlapModel) -> tuple[dict[str, dict[str, list]], str | None]:
    text = (filter_text or "").strip()
    if not text:
        return {}, None

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"Invalid JSON at line {exc.lineno}, column {exc.colno}."

    if not isinstance(raw, dict):
        return {}, "Filter JSON must be an object: {\"field\": [values...]}"

    allowed = get_allowed_filter_fields(model)
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

# ---------------------------------------------------------------------------
# Grid column-definition builders  (driven entirely by the model)
# ---------------------------------------------------------------------------

def _label(name: str) -> str:
    return name.replace("_", " ").title()


def _dim_field_def(col_name: str, dim: DimensionDef, model_id: str) -> dict:
    """Build an AG Grid column def for a dimension attribute or display key."""
    is_display_key = col_name == dim.display_key
    is_key = col_name == dim.dim_key or is_display_key

    label = "Key" if is_key else next(
        (a.label for a in dim.attributes if a.name == col_name),
        _label(col_name),
    )

    col_def = {
        "field": col_name,
        "headerName": label,
        "isDimension": True,
        "sortable": True,
        "filter": False,
        "resizable": True,
        "hide": is_display_key,
        "enablePivot": True,
        "enableRowGroup": True,
    }

    return col_def


def _metric_field_def(metric: MetricDef) -> dict:
    """Build an AG Grid column def for a metric."""
    return {
        "field": metric.name,
        "headerName": metric.label,
        "isDimension": False,
        "sortable": True,
        "filter": False,
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
        "isDimension": False,
        "hide": True,
        "sortable": False,
        "filter": False,
        "resizable": False,
    }


def build_column_defs(
    df: pd.DataFrame,
    model: OlapModel,
    model_id: str,
) -> list[dict]:
    """
    Build the full grouped column-def tree for AG Grid.
    Groups: one per dimension + Metrics. Surrogate keys are hidden leaves.
    """
    defs: list[dict] = []
    known_fields: set[str] = set()

    # Dimension groups
    for dim in model.dimensions:
        children: list[dict] = []
        if dim.display_key in df.columns or 1==1:
            children.append(_dim_field_def(dim.dim_key, dim, model_id))
            known_fields.add(dim.dim_key)
        for attr in dim.attributes:
            if attr.name in df.columns or 1==1:
                children.append(_dim_field_def(attr.name, dim, model_id))
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
                    "toolPanelParams": {"suppressRowGroups": False},
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

    LOGGER.info(
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
        values = get_backend(model_id).filter_values(field_name, max_values=max_values)
        LOGGER.info(
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

initial_model = get_model(DEFAULT_MODEL_ID)
initial_df = get_flat_table(DEFAULT_MODEL_ID)
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
        # Hidden store to track filter model changes
        dcc.Store(id="filter-change-trigger", data={"timestamp": 0, "filterModel": {}}),
        dcc.Store(id="manual-filter-store", data={"timestamp": 0, "filters": {}}),
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
            "Use Columns panel for drilldown. Right-click a data cell for Include/Exclude filter actions.",
            style={"color": "#374151", "marginBottom": "12px"},
        ),
        dcc.Textarea(
            id="server-filter-input",
            value="{}",
            style={"display": "none"},
        ),
        html.Div(
            style={
                "marginBottom": "12px",
                "display": "flex",
                "gap": "10px",
                "alignItems": "flex-end",
                "flexWrap": "wrap",
            },
            children=[
                html.Button("Open Filter JSON", id="open-filter-json-btn", n_clicks=0),
                html.Button("Clear Filters", id="clear-filters-btn", n_clicks=0),
                html.Div(
                    id="filter-parse-message",
                    style={"minWidth": "240px", "fontSize": "13px", "color": "#374151"},
                ),
            ],
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
                    columnDefs=build_column_defs(initial_df, initial_model, DEFAULT_MODEL_ID),
                    eventListeners={
                        "filterChanged": ["onGridFilterChanged(params)"],
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
                )
            ],
        ),
    ],
)


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
    Output("server-filter-input", "value", allow_duplicate=True),
    Input("clear-filters-btn", "n_clicks"),
    prevent_initial_call=True,
)
def on_clear_filters_sync_input(clear_clicks):
    return "{}"


@app.callback(
    Output("manual-filter-store", "data"),
    Output("filter-parse-message", "children"),
    Output("filter-parse-message", "style"),
    Input("clear-filters-btn", "n_clicks"),
    Input("server-filter-input", "value"),
    State("model-selector", "value"),
    prevent_initial_call=True,
)
def on_manual_filter_change(clear_clicks, filter_text, model_id: str):
    ctx = dash.callback_context
    triggered = ctx.triggered[0]["prop_id"] if ctx.triggered else ""

    if triggered == "clear-filters-btn.n_clicks":
        return (
            {"timestamp": pd.Timestamp.utcnow().isoformat(), "filters": {}},
            "Filters cleared.",
            {"minWidth": "240px", "fontSize": "13px", "color": "#065f46"},
        )

    parsed, error = parse_manual_filters(filter_text, get_model(model_id))
    if error:
        return (
            no_update,
            error,
            {"minWidth": "240px", "fontSize": "13px", "color": "#b91c1c"},
        )

    return (
        {"timestamp": pd.Timestamp.utcnow().isoformat(), "filters": parsed},
        f"Applied {len(parsed)} filter field(s).",
        {"minWidth": "240px", "fontSize": "13px", "color": "#065f46"},
    )


@app.callback(
    Output("olap-grid", "rowData"),
    Output("olap-grid", "columnDefs"),
    Output("logged-in-user", "children"),
    Input("model-selector", "value"),
    Input("max-rows-input", "value"),
    Input("olap-grid", "columnState"),
    Input("filter-change-trigger", "data"),
    Input("manual-filter-store", "data"),
)
def on_grid_state_change(model_id: str, max_rows_value, column_state, filter_trigger, manual_filter_data):
    """
    Single unified callback — fires on every grid state change:
      - model selector, max rows, column grouping/pivot, filter selections.

    Rebuilds column defs only when the model changes; always re-queries backend.
    All filtering and grouping is resolved server-side — AG Grid never filters locally.
    """
    ctx = dash.callback_context
    triggered = {t["prop_id"] for t in ctx.triggered}

    print(f"[on_grid_state_change] triggered={triggered}", flush=True)
    print(f"[on_grid_state_change] filter_trigger={filter_trigger}", flush=True)
    print(f"[on_grid_state_change] manual_filter_data={manual_filter_data}", flush=True)

    filter_model = {}
    
    # Try Store first
    if isinstance(filter_trigger, dict):
        candidate = filter_trigger.get("filterModel")
        if isinstance(candidate, dict):
            filter_model = candidate
            print(f"[on_grid_state_change] using filterModel from Store: {filter_model}", flush=True)

    manual_filters: dict[str, dict[str, list]] = {}
    if isinstance(manual_filter_data, dict):
        candidate = manual_filter_data.get("filters")
        if isinstance(candidate, dict):
            manual_filters = candidate
    
    selected_model = get_model(model_id)
    request = build_request_from_grid_state(column_state, max_rows_value)
    request.filters = build_filters_from_filter_model(filter_model)
    request.filters.update(manual_filters)

    LOGGER.info(
        "Grid state change: model=%s rows=%s pivots=%s filters=%s max_rows=%s trigger=%s raw_filter_model=%s",
        model_id,
        request.rows,
        request.columns,
        dict(request.filters),
        request.max_rows,
        triggered,
        filter_model,
    )

    result_df = get_backend(model_id).execute(request)

    # Rebuild column defs only when model changes.
    # Rebuilding defs on columnState events can cause AG Grid to emit a second
    # columnState change while it reapplies column metadata.
    rebuild_cols = "model-selector.value" in triggered
    new_col_defs = build_column_defs(result_df, selected_model, model_id) if rebuild_cols else dash.no_update

    return result_df.to_dict("records"), new_col_defs, get_logged_in_user(model_id)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8050)
