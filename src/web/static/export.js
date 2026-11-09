/* 导出页：勾表 / 选列 / 实时统计 */
(function () {
  const form = document.getElementById("export-form");
  if (!form) return;

  const sheetInputs = () => Array.from(form.querySelectorAll('input[name="sheets"]'));
  const colGroups = () => Array.from(form.querySelectorAll(".exp-col-group"));
  const summaryText = document.getElementById("exp-summary-text");

  /* 表名：data-sheet → 卡片上的标题 */
  const sheetLabel = (key) => {
    const el = form.querySelector(`.exp-sheet[data-sheet="${key}"] .exp-sheet-title`);
    if (!el) return key;
    return el.childNodes[0].textContent.trim();
  };

  function sheetRows(key) {
    const inp = form.querySelector(`input[name="sheets"][value="${key}"]`);
    return inp ? parseInt(inp.dataset.rows || "0", 10) : 0;
  }

  function updateSummary() {
    const picked = sheetInputs().filter((i) => i.checked);
    if (!picked.length) {
      summaryText.textContent = "还没勾表 —— 至少勾一张";
      document.getElementById("exp-submit").disabled = true;
      return;
    }
    document.getElementById("exp-submit").disabled = false;
    const names = picked.map((i) => sheetLabel(i.value));
    let rows = 0;
    picked.forEach((i) => { rows += sheetRows(i.value); });
    const cols = picked.map((i) => {
      const g = form.querySelector(`.exp-col-group[data-sheet="${i.value}"]`);
      if (!g) return "";
      const all = g.querySelectorAll('input[type="checkbox"]');
      const on = g.querySelectorAll('input[type="checkbox"]:checked');
      return `${i.value === "items" ? "科目" : i.value === "indicators" ? "指标" :
              i.value === "entries" ? "条目" : "来源"} ${on.length}/${all.length} 列`;
    }).filter(Boolean);
    summaryText.textContent =
      `将导出 ${picked.length} 张表（${names.join("、")}）· 约 ${rows} 行 · ${cols.join(" · ")}`;
  }

  /* 勾表时联动列分组的显隐 */
  function syncVisibility() {
    const on = new Set(sheetInputs().filter((i) => i.checked).map((i) => i.value));
    colGroups().forEach((g) => {
      const key = g.dataset.sheet;
      g.style.opacity = on.has(key) ? "1" : "0.42";
      g.querySelectorAll('input[type="checkbox"]').forEach((c) => {
        c.disabled = !on.has(key);
      });
    });
    updateSummary();
  }

  /* 全选 / 清空某张表的列 */
  form.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-col-toggle]");
    if (!btn) return;
    e.preventDefault();
    const g = form.querySelector(`.exp-col-group[data-sheet="${btn.dataset.colToggle}"]`);
    if (!g) return;
    const boxes = Array.from(g.querySelectorAll('input[type="checkbox"]:not(:disabled)'));
    const allOn = boxes.every((b) => b.checked);
    boxes.forEach((b) => { b.checked = !allOn; });
    updateSummary();
  });

  /* 默认列：页面加载时把 data-default=1 的勾上 */
  function applyDefaults() {
    form.querySelectorAll(".exp-col-group").forEach((g) => {
      g.querySelectorAll('input[type="checkbox"]').forEach((c) => {
        if (c.dataset.default === "1") c.checked = true;
      });
    });
  }

  /* 重置：回到默认状态 */
  const reset = document.getElementById("exp-reset");
  if (reset) {
    reset.addEventListener("click", () => {
      form.querySelectorAll('input[name="sheets"]').forEach((i) => { i.checked = true; });
      form.querySelectorAll(".exp-col-group").forEach((g) => {
        g.querySelectorAll('input[type="checkbox"]').forEach((c) => {
          c.checked = c.dataset.default === "1";
        });
      });
      form.querySelectorAll('input[name="companies"], input[name="periods"]')
        .forEach((c) => { c.checked = false; });
      form.querySelectorAll('input[name="types"]').forEach((c) => { c.checked = true; });
      const kw = form.querySelector('input[name="keyword"]');
      if (kw) kw.value = "";
      const df = form.querySelector('input[name="date_from"]');
      const dt = form.querySelector('input[name="date_to"]');
      if (df) df.value = "";
      if (dt) dt.value = "";
      syncVisibility();
    });
  }

  form.addEventListener("change", (e) => {
    if (e.target.name === "sheets") syncVisibility();
    else updateSummary();
  });

  applyDefaults();
  syncVisibility();
})();
