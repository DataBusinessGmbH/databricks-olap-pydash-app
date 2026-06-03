var dagfuncs = window.dashAgGridFunctions = window.dashAgGridFunctions || {};

console.info("[dashAgGridFunctions] asset loaded");

(function () {
  const filterValuesCache = {};

  dagfuncs.buildLazyFilterParams = function (modelId, maxValues) {
    const resolvedMaxValues = maxValues || 500;
    return {
      modelId: modelId,
      maxValues: resolvedMaxValues,
      refreshValuesOnOpen: true,
      values: function (params) {
        dagfuncs.lazyFilterValues(params, {
          modelId: modelId,
          maxValues: resolvedMaxValues,
        });
      },
    };
  };

  dagfuncs.lazyFilterValues = function (params, options) {
    const opts = options || {};
    const colDef = (params && params.colDef) || {};
    const filterParams = (params && params.filterParams) || colDef.filterParams || {};
    const modelId = opts.modelId || filterParams.modelId;
    const fieldName =
      colDef.field ||
      (params && params.column && typeof params.column.getColId === "function"
        ? params.column.getColId()
        : undefined);
    const maxValues = opts.maxValues || filterParams.maxValues || 500;

    console.info("[lazyFilterValues] invoked", {
      hasParams: !!params,
      fieldName,
      modelId,
    });

    console.info("[lazyFilterValues] starting");

    let done = false;

    const emit = function (values) {
      if (done) {
        return;
      }
      done = true;
      if (params && typeof params.success === "function") {
        params.success(values);
        return;
      }
      if (params && typeof params.successCallback === "function") {
        params.successCallback(values);
        return;
      }
      if (params && typeof params.setValues === "function") {
        params.setValues(values);
        return;
      }
      // Last-resort no-op: avoid throwing if callback contract differs.
    };

    console.info("[lazyFilterValues] starting", {
      modelId,
      fieldName,
    });

    const timeout = setTimeout(function () {
      console.warn("[lazyFilterValues] timeout; returning empty values", {
        modelId,
        fieldName,
      });
      emit([]);
    }, 7000);

    try {
      if (!modelId || !fieldName) {
        console.warn("[lazyFilterValues] missing modelId/fieldName", {
          modelId,
          fieldName,
        });
        clearTimeout(timeout);
        emit([]);
        return;
      }

      const cacheKey = `${modelId}::${fieldName}::${maxValues}`;
      if (Object.prototype.hasOwnProperty.call(filterValuesCache, cacheKey)) {
        console.info("[lazyFilterValues] cache hit", {
          modelId,
          fieldName,
          count: filterValuesCache[cacheKey].length,
        });
        clearTimeout(timeout);
        emit(filterValuesCache[cacheKey]);
        return;
      }

      const url = `/api/filter-values?model_id=${encodeURIComponent(modelId)}&field_name=${encodeURIComponent(fieldName)}&max_values=${encodeURIComponent(maxValues)}`;
      console.info("[lazyFilterValues] fetching", { modelId, fieldName, maxValues, url });
      fetch(url)
        .then((resp) => {
          if (!resp.ok) {
            throw new Error(`HTTP ${resp.status}`);
          }
          return resp.json();
        })
        .then((payload) => {
          clearTimeout(timeout);
          const values = Array.isArray(payload && payload.values) ? payload.values : [];
          filterValuesCache[cacheKey] = values;
          console.info("[lazyFilterValues] loaded", {
            modelId,
            fieldName,
            count: values.length,
          });
          emit(values);
        })
        .catch((error) => {
          console.error("[lazyFilterValues] fetch failed", {
            modelId,
            fieldName,
            error: String(error),
          });
          clearTimeout(timeout);
          emit([]);
        });
    } catch (e) {
      console.error("[lazyFilterValues] unexpected error", {
        modelId,
        fieldName,
        error: String(e),
      });
      clearTimeout(timeout);
      emit([]);
    }
  };

  // Expose as global symbol for Dash AG Grid function expression resolution.
  window.lazyFilterValues = dagfuncs.lazyFilterValues;
  window.buildLazyFilterParams = dagfuncs.buildLazyFilterParams;
})();

// Clientside callback to detect AG Grid filter changes and forward to store
dagfuncs.onGridFilterChanged = function (params) {
  console.info("[onGridFilterChanged] FIRED", { params });
  try {
    const filterModel =
      params && params.api && typeof params.api.getFilterModel === "function"
        ? params.api.getFilterModel() || {}
        : {};

    console.info("[onGridFilterChanged] extracted filterModel", {
      filterModel,
      timestamp: Date.now(),
    });

    // Try to update Store via Dash's set_props if available
    if (window.dash_clientside && typeof window.dash_clientside.set_props === "function") {
      console.info("[onGridFilterChanged] updating via dash_clientside.set_props");
      window.dash_clientside.set_props("filter-change-trigger", {
        data: {
          timestamp: Date.now(),
          filterModel: filterModel,
        },
      });
      return;
    }

    console.warn("[onGridFilterChanged] dash_clientside.set_props not available");
    console.warn("[onGridFilterChanged] no Store available");
  } catch (e) {
    console.error("[onGridFilterChanged] failed", String(e), e);
  }
};

dagfuncs.readCurrentFilters = function () {
  const textarea = document.getElementById("server-filter-input");
  if (!textarea || !textarea.value) {
    return {};
  }
  try {
    const parsed = JSON.parse(textarea.value);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed;
    }
  } catch (e) {
    // Ignore malformed content and fall back to empty object.
  }
  return {};
};

dagfuncs.writeCurrentFilters = function (currentFilters) {
  const pretty = JSON.stringify(currentFilters, null, 2);
  const filterCount = Object.keys(currentFilters || {}).length;
  if (window.dash_clientside && typeof window.dash_clientside.set_props === "function") {
    window.dash_clientside.set_props("server-filter-input", { value: pretty });
    window.dash_clientside.set_props("manual-filter-store", {
      data: {
        timestamp: Date.now(),
        filters: currentFilters,
      },
    });
    window.dash_clientside.set_props("open-filter-json-btn", {
      children: `Applied filters (${filterCount})`,
    });
    window.dash_clientside.set_props("filter-parse-message", {
      children: "",
      style: { minWidth: "240px", fontSize: "13px", color: "#374151" },
    });
  }
};

dagfuncs.normalizeFilterSpec = function (spec) {
  let existingIn = [];
  let existingOut = [];
  if (Array.isArray(spec)) {
    existingIn = spec;
  } else if (spec && typeof spec === "object") {
    const maybeIn = spec.in;
    const maybeOut = spec.not_in;
    existingIn = Array.isArray(maybeIn) ? maybeIn : (maybeIn ? [maybeIn] : []);
    existingOut = Array.isArray(maybeOut) ? maybeOut : (maybeOut ? [maybeOut] : []);
  }
  return { existingIn, existingOut };
};

dagfuncs.applySingleCellFilter = function (field, rawValue, mode) {
  const value = rawValue === null || rawValue === undefined ? "" : String(rawValue).trim();
  if (!field || !value) {
    return;
  }

  const dedupe = (arr) => Array.from(new Set(arr.filter((v) => v && String(v).trim().length > 0)));
  const currentFilters = dagfuncs.readCurrentFilters();
  const normalized = dagfuncs.normalizeFilterSpec(currentFilters[field]);

  let inVals = dedupe(normalized.existingIn.map((v) => String(v).trim()));
  let outVals = dedupe(normalized.existingOut.map((v) => String(v).trim()));

  if (mode === "include") {
    inVals = dedupe([...inVals, value]);
    outVals = outVals.filter((v) => v !== value);
  } else if (mode === "exclude") {
    outVals = dedupe([...outVals, value]);
    inVals = inVals.filter((v) => v !== value);
  }

  if (inVals.length === 0 && outVals.length === 0) {
    delete currentFilters[field];
  } else {
    const spec = {};
    if (inVals.length > 0) {
      spec.in = inVals;
    }
    if (outVals.length > 0) {
      spec.not_in = outVals;
    }
    currentFilters[field] = spec;
  }

  dagfuncs.writeCurrentFilters(currentFilters);
};

dagfuncs.getCustomContextMenuItems = function (params) {
  const column = params && params.column ? params.column : null;
  const colDef = (column && typeof column.getColDef === "function") ? column.getColDef() : (params && params.colDef ? params.colDef : null);
  const field = (column && typeof column.getColId === "function")
    ? column.getColId()
    : (colDef && colDef.field ? colDef.field : null);
  const rawValue = params ? params.value : undefined;

  const defaults = ["copy", "copyWithHeaders", "separator", "export"];
  // AG Grid may not preserve custom metadata (like isDimension) in context menu params.
  // Offer actions for any concrete data column cell.
  if (!field || field === "ag-Grid-AutoColumn") {
    return defaults;
  }

  const currentFilters = dagfuncs.readCurrentFilters();
  const hasExisting = !!currentFilters[field];
  const labelValue = rawValue === null || rawValue === undefined ? "" : String(rawValue);

  const items = [];
  if (labelValue.trim().length > 0) {
    items.push({
      name: `Include \"${labelValue}\"`,
      action: function () {
        dagfuncs.applySingleCellFilter(field, labelValue, "include");
      },
    });
    items.push({
      name: `Exclude \"${labelValue}\"`,
      action: function () {
        dagfuncs.applySingleCellFilter(field, labelValue, "exclude");
      },
    });
    items.push("separator");
  }

  items.push({
    name: "Edit field filter...",
    action: function () {
      if (params && params.column) {
        dagfuncs.openFieldFilterDialog({ column: params.column });
      }
    },
  });

  if (hasExisting) {
    items.push({
      name: "Clear field filter",
      action: function () {
        const nextFilters = dagfuncs.readCurrentFilters();
        delete nextFilters[field];
        dagfuncs.writeCurrentFilters(nextFilters);
      },
    });
  }

  items.push("separator", ...defaults);
  return items;
};

dagfuncs.openIncludeExcludeDialog = function (headerName, defaultInText, defaultOutText) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.style.position = "fixed";
    overlay.style.inset = "0";
    overlay.style.background = "rgba(0, 0, 0, 0.35)";
    overlay.style.display = "flex";
    overlay.style.alignItems = "center";
    overlay.style.justifyContent = "center";
    overlay.style.zIndex = "9999";

    const modal = document.createElement("div");
    modal.style.width = "680px";
    modal.style.maxWidth = "95vw";
    modal.style.background = "#ffffff";
    modal.style.borderRadius = "10px";
    modal.style.padding = "14px";
    modal.style.boxShadow = "0 10px 30px rgba(0, 0, 0, 0.2)";
    modal.style.fontFamily = "Arial, sans-serif";
    modal.style.boxSizing = "border-box";

    const title = document.createElement("div");
    title.textContent = `Set filters for ${headerName}`;
    title.style.fontWeight = "700";
    title.style.marginBottom = "10px";

    const includeLabel = document.createElement("div");
    includeLabel.textContent = "Include values (comma-separated)";
    includeLabel.style.fontSize = "12px";
    includeLabel.style.marginBottom = "4px";

    const includeInput = document.createElement("input");
    includeInput.type = "text";
    includeInput.value = defaultInText || "";
    includeInput.style.width = "100%";
    includeInput.style.maxWidth = "100%";
    includeInput.style.boxSizing = "border-box";
    includeInput.style.padding = "8px";
    includeInput.style.marginBottom = "10px";
    includeInput.style.border = "1px solid #d1d5db";
    includeInput.style.borderRadius = "6px";

    const excludeLabel = document.createElement("div");
    excludeLabel.textContent = "Exclude values (comma-separated)";
    excludeLabel.style.fontSize = "12px";
    excludeLabel.style.marginBottom = "4px";

    const excludeInput = document.createElement("input");
    excludeInput.type = "text";
    excludeInput.value = defaultOutText || "";
    excludeInput.style.width = "100%";
    excludeInput.style.maxWidth = "100%";
    excludeInput.style.boxSizing = "border-box";
    excludeInput.style.padding = "8px";
    excludeInput.style.marginBottom = "12px";
    excludeInput.style.border = "1px solid #d1d5db";
    excludeInput.style.borderRadius = "6px";

    const buttonRow = document.createElement("div");
    buttonRow.style.display = "flex";
    buttonRow.style.justifyContent = "flex-end";
    buttonRow.style.gap = "8px";

    const cancelBtn = document.createElement("button");
    cancelBtn.textContent = "Cancel";
    cancelBtn.style.padding = "6px 10px";

    const applyBtn = document.createElement("button");
    applyBtn.textContent = "Apply";
    applyBtn.style.padding = "6px 10px";

    buttonRow.appendChild(cancelBtn);
    buttonRow.appendChild(applyBtn);

    modal.appendChild(title);
    modal.appendChild(includeLabel);
    modal.appendChild(includeInput);
    modal.appendChild(excludeLabel);
    modal.appendChild(excludeInput);
    modal.appendChild(buttonRow);
    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    const close = (result) => {
      if (overlay.parentNode) {
        overlay.parentNode.removeChild(overlay);
      }
      resolve(result);
    };

    applyBtn.addEventListener("click", () => {
      close({
        confirmed: true,
        includeText: includeInput.value || "",
        excludeText: excludeInput.value || "",
      });
    });

    cancelBtn.addEventListener("click", () => close({ confirmed: false }));
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) {
        close({ confirmed: false });
      }
    });

    includeInput.focus();
  });
};

dagfuncs.openFieldFilterDialog = async function (params) {
  try {
    const column = params && params.column;
    const colDef = column && typeof column.getColDef === "function" ? column.getColDef() : null;
    const field = colDef && colDef.field;
    const isDimension = !!(colDef && colDef.isDimension);

    if (!field || !isDimension) {
      return;
    }

    const currentFilters = dagfuncs.readCurrentFilters();

    const existingSpec = currentFilters[field];
    const normalized = dagfuncs.normalizeFilterSpec(existingSpec);
    const existingIn = normalized.existingIn;
    const existingOut = normalized.existingOut;

    const defaultInText = existingIn.join(", ");
    const defaultOutText = existingOut.join(", ");
    const headerName = (colDef && colDef.headerName) || field;

    const dialogResult = await dagfuncs.openIncludeExcludeDialog(
      headerName,
      defaultInText,
      defaultOutText,
    );

    if (!dialogResult || !dialogResult.confirmed) {
      return;
    }

    const includeInput = dialogResult.includeText;
    const excludeInput = dialogResult.excludeText;

    const includeTokens = includeInput
      .split(",")
      .map((v) => v.trim())
      .filter((v) => v.length > 0);

    const excludeTokens = excludeInput
      .split(",")
      .map((v) => v.trim())
      .filter((v) => v.length > 0);

    const dedupe = (arr) => Array.from(new Set(arr.filter((v) => v && v.length > 0)));
    const inVals = dedupe(includeTokens);
    const outVals = dedupe(excludeTokens);

    if (inVals.length === 0 && outVals.length === 0) {
      delete currentFilters[field];
    } else {
      const spec = {};
      if (inVals.length > 0) {
        spec.in = inVals;
      }
      if (outVals.length > 0) {
        spec.not_in = outVals;
      }
      currentFilters[field] = spec;
    }

    dagfuncs.writeCurrentFilters(currentFilters);
  } catch (e) {
    console.error("[openFieldFilterDialog] failed", String(e), e);
  }
};

// Expose for AG Grid dashGridOptions callback usage.
window.onGridFilterChanged = dagfuncs.onGridFilterChanged;
window.getCustomContextMenuItems = dagfuncs.getCustomContextMenuItems;
