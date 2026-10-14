// Small progressive enhancements: grid filter/sort, clickable rows, dialogs, TASKS menus.
(function () {
  "use strict";

  // Filter box: <input data-filter="tableId">
  document.querySelectorAll("input[data-filter]").forEach(function (input) {
    var table = document.getElementById(input.dataset.filter);
    if (!table) return;
    var counter = document.querySelector('[data-count-for="' + input.dataset.filter + '"]');
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase(), shown = 0;
      table.querySelectorAll("tbody tr:not(.empty)").forEach(function (tr) {
        var hit = !q || tr.textContent.toLowerCase().indexOf(q) !== -1;
        tr.hidden = !hit;
        if (hit) shown++;
      });
      if (counter) counter.textContent = shown;
    });
  });

  // Sortable grids: <table class="grid sortable">
  document.querySelectorAll("table.sortable").forEach(function (table) {
    table.querySelectorAll("thead th").forEach(function (th, idx) {
      if (th.classList.contains("nosort")) return;
      th.addEventListener("click", function () {
        var tbody = table.tBodies[0];
        var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr:not(.empty)"));
        var asc = th.dataset.dir !== "asc";
        table.querySelectorAll("thead th").forEach(function (o) { delete o.dataset.dir; });
        th.dataset.dir = asc ? "asc" : "desc";
        rows.sort(function (a, b) {
          var x = (a.cells[idx].dataset.sort || a.cells[idx].textContent).trim();
          var y = (b.cells[idx].dataset.sort || b.cells[idx].textContent).trim();
          var nx = parseFloat(x), ny = parseFloat(y);
          var c = (!isNaN(nx) && !isNaN(ny) && String(nx) === x && String(ny) === y) ? nx - ny : x.localeCompare(y);
          return asc ? c : -c;
        });
        rows.forEach(function (r) { tbody.appendChild(r); });
      });
    });
  });

  // Clickable rows: <tr data-href="...">
  document.addEventListener("click", function (e) {
    var tr = e.target.closest("tr[data-href]");
    if (!tr || e.target.closest("a, button, input, select, label, form")) return;
    window.location = tr.dataset.href;
  });

  // Dialogs: <button data-open="dlgId">, <button data-close>
  document.addEventListener("click", function (e) {
    var opener = e.target.closest("[data-open]");
    if (opener) {
      e.preventDefault();
      var dlg = document.getElementById(opener.dataset.open);
      if (dlg) { closeMenus(); dlg.showModal(); var f = dlg.querySelector("input:not([type=hidden]), textarea, select"); if (f) f.focus(); }
      return;
    }
    var closer = e.target.closest("[data-close]");
    if (closer) { e.preventDefault(); closer.closest("dialog").close(); }
  });
  document.querySelectorAll("dialog[data-autoopen]").forEach(function (d) { d.showModal(); });

  // TASKS menus (<details class="menu">): one open at a time, close on outside click.
  function closeMenus(except) {
    document.querySelectorAll("details.menu[open]").forEach(function (d) { if (d !== except) d.open = false; });
  }
  document.addEventListener("click", function (e) {
    var menu = e.target.closest("details.menu");
    closeMenus(menu);
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeMenus(); });

  // Confirm: <form data-confirm="...">; in batch forms "{n}" becomes the number of selected rows.
  document.addEventListener("submit", function (e) {
    var msg = e.target.dataset.confirm;
    if (msg && e.target.hasAttribute("data-batch")) msg = msg.replace("{n}", batchChecked(e.target).length);
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Batch management dialogs: <form data-batch> with input[name=ids] checkboxes and a select-all box.
  function batchChecked(form) { return form.querySelectorAll("input[name=ids]:checked"); }
  function batchSync(form) {
    var boxes = form.querySelectorAll("input[name=ids]"), n = batchChecked(form).length;
    boxes.forEach(function (b) { b.closest("tr").classList.toggle("picked", b.checked); });
    var all = form.querySelector("[data-batch-all]");
    if (all) { all.checked = n > 0 && n === boxes.length; all.indeterminate = n > 0 && n < boxes.length; }
    var count = form.querySelector("[data-batch-count]");
    if (count) count.textContent = n;
    var submit = form.querySelector("[data-batch-submit]");
    if (submit) submit.disabled = n === 0;
  }
  document.querySelectorAll("form[data-batch]").forEach(function (form) {
    form.addEventListener("change", function (e) {
      if (e.target.matches("[data-batch-all]")) {
        form.querySelectorAll("input[name=ids]").forEach(function (b) { if (!b.closest("tr").hidden) b.checked = e.target.checked; });
      }
      batchSync(form);
    });
    // Clicking anywhere on a row toggles its checkbox.
    form.addEventListener("click", function (e) {
      var tr = e.target.closest("tbody tr");
      if (!tr || e.target.closest("a, button, input, label")) return;
      var box = tr.querySelector("input[name=ids]");
      if (box) { box.checked = !box.checked; batchSync(form); }
    });
    form.closest("dialog").addEventListener("close", function () { form.reset(); batchSync(form); });
    batchSync(form);
  });

  // Live task board: <div data-task-panel="<board key>" data-version="..."> is re-fetched every 10 s
  // (204 = unchanged). Skipped while a task dialog is open or the tab is hidden, so nothing being typed is lost.
  var TASK_POLL_MS = 10000;
  setInterval(function () {
    var panel = document.querySelector("[data-task-panel]");
    if (!panel || document.hidden || document.querySelector("dialog[open]")) return;
    var url = "/task-panel?board=" + encodeURIComponent(panel.dataset.taskPanel) + "&v=" + encodeURIComponent(panel.dataset.version);
    fetch(url, { credentials: "same-origin" }).then(function (r) {
      return r.status === 200 ? r.text() : null;
    }).then(function (html) {
      if (!html || document.querySelector("dialog[open]")) return;
      var tmp = document.createElement("div");
      tmp.innerHTML = html;
      var fresh = tmp.querySelector("[data-task-panel]");
      if (fresh) panel.replaceWith(fresh);
    }).catch(function () {});
  }, TASK_POLL_MS);

  // Copy buttons: <button data-copy="elementId">
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-copy]");
    if (!b) return;
    e.preventDefault();
    var el = document.getElementById(b.dataset.copy);
    if (el && navigator.clipboard) navigator.clipboard.writeText(el.textContent.trim()).then(function () {
      var t = b.textContent; b.textContent = b.dataset.copied || "Copied"; setTimeout(function () { b.textContent = t; }, 1200);
    });
  });
})();
