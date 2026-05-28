from pathlib import Path

import dash_ag_grid as dag
import pandas as pd
from dash import Dash, html


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
METRICS = ["sales_amount", "quantity"]
KEYS = ["date_key", "product_key", "customer_key"]

DIMENSION_KEYS = {
    "Date": {"source": "date_key", "alias": "date_dim_key"},
    "Product": {"source": "product_key", "alias": "product_dim_key"},
    "Customer": {"source": "customer_key", "alias": "customer_dim_key"},
}

DIMENSIONS = {
    "Date": ["year", "quarter", "month"],
    "Product": ["product_name", "category", "brand"],
    "Customer": ["customer_name", "segment", "region"],
}


def load_star_schema() -> pd.DataFrame:
    fact = pd.read_csv(DATA_DIR / "fact_sales.csv")
    dim_date = pd.read_csv(DATA_DIR / "dim_date.csv")
    dim_product = pd.read_csv(DATA_DIR / "dim_product.csv")
    dim_customer = pd.read_csv(DATA_DIR / "dim_customer.csv")

    df = fact.merge(dim_date, on="date_key", how="left")
    df = df.merge(dim_product, on="product_key", how="left")
    df = df.merge(dim_customer, on="customer_key", how="left")

    for dimension_name, key_spec in DIMENSION_KEYS.items():
        df[key_spec["alias"]] = df[key_spec["source"]]

    return df


STAR_DF = load_star_schema()


def _label(name: str) -> str:
    return name.replace("_", " ").title()


def _dimension_members(dimension_name: str) -> list[str]:
    key_alias = DIMENSION_KEYS[dimension_name]["alias"]
    return [key_alias, *DIMENSIONS[dimension_name]]


def _display_label(column_name: str) -> str:
    if column_name in {spec["alias"] for spec in DIMENSION_KEYS.values()}:
        return "Key"
    return _label(column_name)


def _field_def(column_name: str) -> dict:
    col_def = {
        "field": column_name,
        "headerName": _display_label(column_name),
        "sortable": True,
        "filter": True,
        "resizable": True,
    }

    if column_name in KEYS:
        col_def["hide"] = True

    if column_name in METRICS:
        col_def.update(
            {
                "type": "numericColumn",
                "enableValue": True,
                "allowedAggFuncs": ["sum", "avg", "min", "max", "count"],
                "aggFunc": "sum",
            }
        )
    else:
        col_def.update({"enablePivot": True})

    return col_def


def _column_defs(columns: list[str]) -> list[dict]:
    defs: list[dict] = []

    for dim_name in DIMENSIONS:
        children = [_field_def(attr) for attr in _dimension_members(dim_name) if attr in columns]
        if children:
            defs.append({"headerName": dim_name, "children": children})

    metric_children = [_field_def(metric) for metric in METRICS if metric in columns]
    if metric_children:
        defs.append({"headerName": "Metrics", "children": metric_children})

    grouped_fields = set(KEYS + METRICS)
    for dim_name in DIMENSIONS:
        grouped_fields.update(_dimension_members(dim_name))

    other_children = [_field_def(col) for col in columns if col not in grouped_fields]
    if other_children:
        defs.append({"headerName": "Other Attributes", "children": other_children})

    return defs


def _grid_options() -> dict:
    return {
        "animateRows": True,
        "pivotPanelShow": "always",
        "sideBar": {
            "position": "right",
            "toolPanels": [
                {
                    "id": "columns",
                    "labelDefault": "Columns",
                    "labelKey": "columns",
                    "iconKey": "columns",
                    "toolPanel": "agColumnsToolPanel",
                    "toolPanelParams": {
                        "suppressRowGroups": True,
                    },
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


app = Dash(__name__)

app.layout = html.Div(
    style={"fontFamily": "Arial, sans-serif", "padding": "16px", "width": "80vw", "maxWidth": "80vw", "margin": "0 auto"},
    children=[
        html.H2("OLAP Slice & Dice (Drag & Drop)"),
        html.Div(
            "Use the right-side collapsible panel to switch between Columns and Filters while working in the grid.",
            style={"color": "#374151", "marginBottom": "12px"},
        ),
        html.Div(
            style={"display": "grid", "gridTemplateColumns": "1fr", "gap": "16px", "alignItems": "start"},
            children=[
                html.Div(
                    style={"border": "1px solid #d1d5db", "borderRadius": "8px", "padding": "10px", "background": "#fff"},
                    children=[
                        dag.AgGrid(
                            id="olap-grid",
                            rowData=STAR_DF.to_dict("records"),
                            columnDefs=_column_defs(STAR_DF.columns.tolist()),
                            defaultColDef={"flex": 1, "minWidth": 120, "filter": True, "floatingFilter": True},
                            enableEnterpriseModules=True,
                            className="ag-theme-alpine",
                            dashGridOptions=_grid_options(),
                            style={"height": "78vh", "width": "100%"},
                        )
                    ],
                ),
            ],
        ),
    ],
)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8050)
