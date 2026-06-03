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
from flask import jsonify, request as flask_request

import dash_ag_grid as dag
import pandas as pd
from dash import Dash, Input, Output, State, dcc, html, no_update
import dash

from src.model import OlapModel, DimensionDef, MetricDef
from src import db as db_layer
from src.db import (
    LOGGER,
    OlapDatabase,
    OlapQueryRequest,
    create_databricks_backend,
)
import logging

# ---------------------------------------------------------------------------
# Bootstrap: load model, connect database layer
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
MODELS_DIR = BASE_DIR / "models"
RUNTIME_MODELS_DIR = BASE_DIR / ".runtime_models"
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

    table_models = load_models_from_env_table()
    if table_models:
        RUNTIME_MODELS_DIR.mkdir(parents=True, exist_ok=True)
        for model_name, model_yaml in table_models.items():
            safe_stem = re.sub(r"[^A-Za-z0-9_.-]", "_", model_name).strip("._-") or "model"
            model_path = RUNTIME_MODELS_DIR / f"{safe_stem}.yaml"
            model_path.write_text(model_yaml, encoding="utf-8")
            model_files[model_name] = model_path

    if not model_files:
        raise FileNotFoundError(
            "No model YAML found. Add one under models/*.yaml."
        )
    
    LOGGER.info(f"Discovered model files: {model_files}")

    return model_files


def _quoted_ident(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _run_startup_sql(sql: str) -> pd.DataFrame:
    cfg = db_layer.load_databricks_config_from_env()

    if cfg.mode == "sql":
        missing = [
            key
            for key, value in {
                "DATABRICKS_SERVER_HOSTNAME": cfg.server_hostname,
                "DATABRICKS_HTTP_PATH": cfg.http_path,
                "DATABRICKS_TOKEN": cfg.access_token,
            }.items()
            if not value
        ]
        if missing:
            LOGGER.warning("Skipping models table lookup (missing SQL env vars): %s", ", ".join(missing))
            return pd.DataFrame()

        sql_mod = importlib.import_module("databricks.sql")
        conn = sql_mod.connect(
            server_hostname=cfg.server_hostname,
            http_path=cfg.http_path,
            access_token=cfg.access_token,
        )
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return pd.DataFrame(rows, columns=cols)
        finally:
            conn.close()

    db_connect = importlib.import_module("databricks.connect")
    spark = db_connect.DatabricksSession.builder.serverless().getOrCreate()
    return spark.sql(sql).toPandas()


def load_models_from_env_table() -> dict[str, str]:
    """
    Load models from the environment-configured table if configured.

    Expected columns:
      - ModelName
      - ModelDefinition
    """
    catalog = os.getenv("MODELS_TABLE_CATALOG")
    schema = os.getenv("MODELS_TABLE_SCHEMA")
    table = os.getenv("MODELS_TABLE_NAME")

    if not (catalog and schema and table):
        LOGGER.info("Model_table_catalog = %s, Model_table_schema = %s, Model_table_name = %s", catalog, schema, table)        
        LOGGER.info("Models table env vars not fully set; skipping table model discovery.")
        return {}

    qualified = ".".join([_quoted_ident(catalog), _quoted_ident(schema), _quoted_ident(table)])
    sql = f"SELECT * FROM {qualified}"

    try:
        df = _run_startup_sql(sql)
    except Exception:
        STARTUP_WARNINGS.append(
            f"Configured models table {catalog}.{schema}.{table} was not found or could not be read. Continuing with local model files."
        )
        LOGGER.info("Failed reading models from table %s; continuing with local model files.", qualified)
        return {}

    if df.empty:
        LOGGER.info("No rows found in models table %s", qualified)
        return {}

    lower_to_actual = {str(c).strip().lower(): c for c in df.columns}
    name_col = lower_to_actual.get("modelname")
    def_col = lower_to_actual.get("modeldefinition")

    if not name_col or not def_col:
        LOGGER.warning(
            "Models table %s missing expected columns ModelName/ModelDefinition. Found: %s",
            qualified,
            list(df.columns),
        )
        return {}

    models: dict[str, str] = {}
    for _, row in df.iterrows():
        model_name = str(row[name_col]).strip() if pd.notna(row[name_col]) else ""
        model_def = str(row[def_col]).strip() if pd.notna(row[def_col]) else ""
        if not model_name or not model_def:
            continue
        models[model_name] = model_def

    LOGGER.info("Loaded %s model definition(s) from table %s", len(models), qualified)
    return models


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


def build_default_request(model: OlapModel, max_rows: int | None = None) -> OlapQueryRequest:
    default_rows: list[str] = []
    default_metrics = [metric.name for metric in model.metrics if metric.default_show]

    for dim in model.dimensions:
        if dim.default_show:
            default_rows.append(dim.dim_key)
        for attr in dim.attributes:
            if attr.default_show:
                default_rows.append(attr.name)

    if not default_rows and not default_metrics:
        return OlapQueryRequest(max_rows=max_rows)

    deduped_rows = list(dict.fromkeys(default_rows))
    return OlapQueryRequest(rows=deduped_rows, metrics=default_metrics, max_rows=max_rows)


def get_default_view_table(model_id: str, max_rows: int | None = None) -> pd.DataFrame:
    model = get_model(model_id)
    request = build_default_request(model, max_rows=max_rows or DEFAULT_MAX_ROWS)
    return get_backend(model_id).execute(request)


def get_default_visible_fields(model: OlapModel) -> set[str]:
    visible_fields: set[str] = set()
    for dim in model.dimensions:
        if dim.default_show:
            visible_fields.add(dim.dim_key)
        for attr in dim.attributes:
            if attr.default_show:
                visible_fields.add(attr.name)
    for metric in model.metrics:
        if metric.default_show:
            visible_fields.add(metric.name)
    return visible_fields


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


def get_dimension_filter_fields(model: OlapModel) -> list[str]:
    fields: set[str] = set()
    for dim in model.dimensions:
        fields.add(dim.dim_key)
        fields.add(dim.display_key)
        for attr in dim.attributes:
            fields.add(attr.name)
    return sorted(fields)


def get_dimension_dropdown_options(model_id: str) -> list[dict[str, str]]:
    model = get_model(model_id)
    return [{"label": dim.name, "value": dim.name} for dim in model.dimensions]


def get_field_filter_dropdown_options(model_id: str, dimension_name: str | None) -> list[dict[str, str]]:
    model = get_model(model_id)
    if not dimension_name:
        return []

    dim = next((d for d in model.dimensions if d.name == dimension_name), None)
    if dim is None:
        return []

    options: list[dict[str, str]] = [{"label": "Key", "value": dim.dim_key}]
    for attr in dim.attributes:
        options.append({"label": attr.label, "value": attr.name})
    return options


def _split_csv_values(value: str | None) -> list[str]:
    parts = [p.strip() for p in (value or "").split(",")]
    return [p for p in parts if p]


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
    default_visible_fields = get_default_visible_fields(model)
    has_default_visibility = bool(default_visible_fields)

    # Dimension groups
    for dim in model.dimensions:
        children: list[dict] = []
        if dim.display_key in df.columns or 1==1:
            key_def = _dim_field_def(dim.dim_key, dim, model_id)
            if has_default_visibility:
                key_def["hide"] = dim.dim_key not in default_visible_fields
            children.append(key_def)
            known_fields.add(dim.dim_key)
        for attr in dim.attributes:
            if attr.name in df.columns or 1==1:
                attr_def = _dim_field_def(attr.name, dim, model_id)
                if has_default_visibility:
                    attr_def["hide"] = attr.name not in default_visible_fields
                children.append(attr_def)
                known_fields.add(attr.name)
        if children:
            defs.append({"headerName": dim.name, "children": children})

    # Metrics group
    metric_children = []
    for metric in model.metrics:
        if metric.name in df.columns:
            metric_def = _metric_field_def(metric)
            if has_default_visibility:
                metric_def["hide"] = metric.name not in default_visible_fields
            metric_children.append(metric_def)
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
initial_df = get_default_view_table(DEFAULT_MODEL_ID)
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
                    style={"maxWidth": "180px", "paddingRight": "16px"},
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
                    style={"minWidth": "240px", "maxWidth": "280px"},
                    children=[
                        html.Div("Dimension", style={"fontWeight": "600", "marginBottom": "6px"}),
                        dcc.Dropdown(
                            id="field-filter-dimension-selector",
                            options=get_dimension_dropdown_options(DEFAULT_MODEL_ID),
                            value=(get_dimension_dropdown_options(DEFAULT_MODEL_ID)[0]["value"] if get_dimension_dropdown_options(DEFAULT_MODEL_ID) else None),
                            clearable=False,
                        ),
                    ],
                ),
                html.Div(
                    style={"minWidth": "240px", "maxWidth": "280px"},
                    children=[
                        html.Div("Attribute", style={"fontWeight": "600", "marginBottom": "6px"}),
                        dcc.Dropdown(
                            id="field-filter-selector",
                            options=get_field_filter_dropdown_options(
                                DEFAULT_MODEL_ID,
                                get_dimension_dropdown_options(DEFAULT_MODEL_ID)[0]["value"] if get_dimension_dropdown_options(DEFAULT_MODEL_ID) else None,
                            ),
                            value=(
                                get_field_filter_dropdown_options(
                                    DEFAULT_MODEL_ID,
                                    get_dimension_dropdown_options(DEFAULT_MODEL_ID)[0]["value"] if get_dimension_dropdown_options(DEFAULT_MODEL_ID) else None,
                                )[0]["value"]
                                if get_field_filter_dropdown_options(
                                    DEFAULT_MODEL_ID,
                                    get_dimension_dropdown_options(DEFAULT_MODEL_ID)[0]["value"] if get_dimension_dropdown_options(DEFAULT_MODEL_ID) else None,
                                )
                                else None
                            ),
                            clearable=False,
                        ),
                    ],
                ),
                html.Div(
                    style={"minWidth": "240px", "paddingRight": "16px"},
                    children=[
                        html.Div("Include values (csv)", style={"fontWeight": "600", "marginBottom": "6px"}),
                        dcc.Input(
                            id="field-filter-include-input",
                            type="text",
                            placeholder="e.g. 2024, 2025",
                            debounce=True,
                            style={"width": "100%", "padding": "8px"},
                        ),
                    ],
                ),
                html.Div(
                    style={"minWidth": "240px", "paddingRight": "16px"},
                    children=[
                        html.Div("Exclude values (csv)", style={"fontWeight": "600", "marginBottom": "6px"}),
                        dcc.Input(
                            id="field-filter-exclude-input",
                            type="text",
                            placeholder="e.g. APAC",
                            debounce=True,
                            style={"width": "100%", "padding": "8px"},
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
                    "Clear Filter",
                    id="clear-field-filter-btn",
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
    Output("field-filter-dimension-selector", "options"),
    Output("field-filter-dimension-selector", "value"),
    Output("field-filter-selector", "options"),
    Output("field-filter-selector", "value"),
    Input("model-selector", "value"),
    Input("field-filter-dimension-selector", "value"),
    State("field-filter-selector", "value"),
)
def on_model_or_dimension_change_update_field_filter_options(
    model_id: str,
    current_dimension: str | None,
    current_field: str | None,
):
    dim_options = get_dimension_dropdown_options(model_id)
    dim_values = {o["value"] for o in dim_options}
    next_dimension = current_dimension if current_dimension in dim_values else (dim_options[0]["value"] if dim_options else None)

    field_options = get_field_filter_dropdown_options(model_id, next_dimension)
    field_values = {o["value"] for o in field_options}
    next_field = current_field if current_field in field_values else (field_options[0]["value"] if field_options else None)
    return dim_options, next_dimension, field_options, next_field


@app.callback(
    Output("server-filter-input", "value", allow_duplicate=True),
    Input("apply-field-filter-btn", "n_clicks"),
    Input("clear-field-filter-btn", "n_clicks"),
    State("field-filter-selector", "value"),
    State("field-filter-include-input", "value"),
    State("field-filter-exclude-input", "value"),
    State("server-filter-input", "value"),
    prevent_initial_call=True,
)
def on_field_filter_apply_or_clear(
    apply_clicks,
    clear_clicks,
    field_name,
    include_csv,
    exclude_csv,
    current_filter_json,
):
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

    if triggered == "clear-field-filter-btn.n_clicks":
        current.pop(field_name, None)
        return json.dumps(current, indent=2)

    include_vals = _split_csv_values(include_csv)
    exclude_vals = _split_csv_values(exclude_csv)

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
            filter_button_label(0),
        )

    parsed, error = parse_manual_filters(filter_text, get_model(model_id))
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
    rebuild_cols = "model-selector.value" in triggered
    request = build_default_request(selected_model, sanitize_max_rows(max_rows_value)) if rebuild_cols else build_request_from_grid_state(column_state, max_rows_value)
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
    new_col_defs = build_column_defs(result_df, selected_model, model_id) if rebuild_cols else dash.no_update

    return result_df.to_dict("records"), new_col_defs, get_logged_in_user(model_id)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8050)
