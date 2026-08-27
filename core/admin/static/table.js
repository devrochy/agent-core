/* Motor data-table client-side (paridad agent-sale, estética Bootstrap).
 *
 * Cada contenedor `[data-table]` tiene:
 *  - `input[data-table-search]`  → búsqueda substring sobre `data-search` de cada fila.
 *  - `select[data-table-filter][data-filter-key]` → filtra contra `data-filter-<key>`.
 *  - `th[data-sort-key]` → ordena por `data-sort-<key>` (asc/desc alternando).
 *  - `[data-table-count]`, `[data-table-pagelabel]`, `[data-table-prev]`, `[data-table-next]`.
 * Las filas visibles son `tr[data-search]`. Todo client-side sobre el dataset del tenant.
 */
(function () {
  function initTable(wrap) {
    var table = wrap.querySelector("table");
    if (!table) return;
    var tbody = table.querySelector("tbody");
    var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr[data-search]"));
    var search = wrap.querySelector("input[data-table-search]");
    var filters = Array.prototype.slice.call(wrap.querySelectorAll("select[data-table-filter]"));
    var sortables = Array.prototype.slice.call(wrap.querySelectorAll("th[data-sort-key]"));
    var pageSize = parseInt(wrap.getAttribute("data-page-size") || "20", 10) || 20;
    var countEl = wrap.querySelector("[data-table-count]");
    var pageLabel = wrap.querySelector("[data-table-pagelabel]");
    var prevBtn = wrap.querySelector("[data-table-prev]");
    var nextBtn = wrap.querySelector("[data-table-next]");

    var sortKey = "";
    var sortDir = 1; // 1 = asc, -1 = desc
    var page = 1;

    function currentRows() {
      var q = search ? search.value.toLowerCase().trim() : "";
      return rows.filter(function (r) {
        if (q && (r.getAttribute("data-search") || "").toLowerCase().indexOf(q) === -1) return false;
        for (var i = 0; i < filters.length; i++) {
          var key = filters[i].getAttribute("data-filter-key");
          var val = filters[i].value;
          if (!key || !val) continue;
          if ((r.getAttribute("data-filter-" + key) || "") !== val) return false;
        }
        return true;
      });
    }

    function sortRows(list) {
      if (!sortKey) return list;
      return list.slice().sort(function (a, b) {
        var av = a.getAttribute("data-sort-" + sortKey) || "";
        var bv = b.getAttribute("data-sort-" + sortKey) || "";
        var an = parseFloat(av), bn = parseFloat(bv);
        var cmp;
        if (!isNaN(an) && !isNaN(bn)) cmp = an - bn;
        else cmp = av.localeCompare(bv, "es", { sensitivity: "base" });
        return cmp * sortDir;
      });
    }

    function render() {
      var matched = sortRows(currentRows());
      var total = matched.length;
      var pages = Math.max(1, Math.ceil(total / pageSize));
      if (page > pages) page = pages;
      var start = (page - 1) * pageSize;
      var visible = matched.slice(start, start + pageSize);
      var seen = {};
      visible.forEach(function (r) { seen[r] = true; });
      rows.forEach(function (r) { r.style.display = seen[r] ? "" : "none"; });
      if (countEl) countEl.textContent = String(total);
      if (pageLabel) pageLabel.textContent = page + " / " + pages;
      if (prevBtn) prevBtn.disabled = page <= 1;
      if (nextBtn) nextBtn.disabled = page >= pages;
    }

    if (search) search.addEventListener("input", function () { page = 1; render(); });
    filters.forEach(function (f) { f.addEventListener("change", function () { page = 1; render(); }); });

    sortables.forEach(function (th) {
      th.style.cursor = "pointer";
      th.addEventListener("click", function () {
        var key = th.getAttribute("data-sort-key");
        if (sortKey === key) sortDir *= -1;
        else { sortKey = key; sortDir = 1; }
        sortables.forEach(function (h) {
          h.classList.remove("dt-asc", "dt-desc");
          if (h === th) h.classList.add(sortDir === 1 ? "dt-asc" : "dt-desc");
        });
        render();
      });
    });

    if (prevBtn) prevBtn.addEventListener("click", function () { if (page > 1) { page--; render(); } });
    if (nextBtn) nextBtn.addEventListener("click", function () {
      var pages = Math.max(1, Math.ceil(currentRows().length / pageSize));
      if (page < pages) { page++; render(); }
    });

    render();
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-table]").forEach(initTable);
  });
})();