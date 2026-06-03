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

    // Fallback: update hidden input to trigger callback
    const hiddenInput = document.getElementById("filter-model-input");
    if (hiddenInput) {
      console.info("[onGridFilterChanged] updating hidden input fallback");
      hiddenInput.value = JSON.stringify({
        timestamp: Date.now(),
        filterModel: filterModel,
      });
      // Trigger input change event
      hiddenInput.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }

    console.warn("[onGridFilterChanged] no Store or hidden input available");
  } catch (e) {
    console.error("[onGridFilterChanged] failed", String(e), e);
  }
};

// Expose for AG Grid dashGridOptions callback usage.
window.onGridFilterChanged = dagfuncs.onGridFilterChanged;
