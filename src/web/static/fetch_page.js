/* 取数页：源下拉、参数表单（when 条件显隐 + options_depends 级联）、后台取数任务卡与结果表 */

let fetchSpecs = [];

function currentSpec() {
  const id = document.getElementById("fetch-source").value;
  return fetchSpecs.find((s) => s.id === id);
}

function currentValues(spec) {
  const out = {};
  (spec.params || []).forEach((p) => {
    const el = document.querySelector(`[data-name="${p.name}"]`);
    if (el) out[p.name] = p.type === "checkbox" ? el.checked : el.value;
    else if (p.default != null) out[p.name] = p.default;
  });
  return out;
}

function resolveOptions(p, values) {
  if (!p.options_depends) return p.options || [];
  let node = p.options_depends.map || {};
  for (const f of p.options_depends.fields || []) {
    if (!node || typeof node !== "object") return [];
    const key = String(values[f] == null ? "" : values[f]);
    node = (key in node) ? node[key] : node["*"];
  }
  return node == null ? null : node;
}

function fieldHTML(p, values) {
  if (p.when && !(p.when.in || []).includes(String(values[p.when.field]))) return "";
  const id = "fp-" + p.name;
  if (p.type === "select") {
    const opts = resolveOptions(p, values);
    if (opts == null) {
      return `<label class="ff"><span>${App.escapeHtml(p.label)}</span>
        <input type="text" id="${id}" data-name="${p.name}" value="${App.escapeHtml(p.default == null ? "" : p.default)}"></label>`;
    }
    const cur = String(values[p.name] == null ? "" : values[p.name]);
    const inOpts = opts.some((o) => String(o) === cur);
    const fallback = opts.some((o) => String(o) === String(p.default))
      ? String(p.default) : String(opts[0] == null ? "" : opts[0]);
    const selVal = inOpts ? cur : fallback;
    const html = opts.map((o) => {
      const label = (p.labels && p.labels[o]) || o;
      const sel = String(o) === selVal ? " selected" : "";
      return `<option value="${App.escapeHtml(o)}"${sel}>${App.escapeHtml(label)}</option>`;
    }).join("");
    return `<label class="ff"><span>${App.escapeHtml(p.label)}</span>
      <select id="${id}" data-name="${p.name}">${html}</select></label>`;
  }
  if (p.type === "checkbox") {
    const checked = values[p.name] ? " checked" : "";
    return `<label class="ff ff-check"><input type="checkbox" id="${id}" data-name="${p.name}"${checked}>
      <span>${App.escapeHtml(p.label)}</span></label>`;
  }
  const type = p.type === "number" ? "number" : "text";
  const req = p.required ? " required" : "";
  const val = values[p.name] == null ? "" : values[p.name];
  return `<label class="ff"><span>${App.escapeHtml(p.label)}${p.required ? " *" : ""}</span>
    <input type="${type}" id="${id}" data-name="${p.name}" value="${App.escapeHtml(val)}"${req}></label>`;
}

function renderParams() {
  const spec = currentSpec();
  const box = document.getElementById("fetch-params");
  if (!spec) { box.innerHTML = ""; return; }
  document.getElementById("fetch-desc").textContent = spec.desc || "";
  const values = currentValues(spec);
  box.innerHTML = (spec.params || []).map((p) => fieldHTML(p, values)).join("");
}

function collectParams(spec) {
  const out = {};
  (spec.params || []).forEach((p) => {
    const el = document.querySelector(`[data-name="${p.name}"]`);
    if (!el) return;
    if (p.type === "checkbox") out[p.name] = el.checked;
    else if (p.type === "number") out[p.name] = el.value === "" ? p.default : Number(el.value);
    else out[p.name] = el.value;
  });
  return out;
}

/* 别的字段以哪些字段为显隐/选项控制器：只有控制器变化才重绘表单 */
function controllerFields(spec) {
  const set = new Set();
  (spec.params || []).forEach((p) => {
    if (p.when && p.when.field) set.add(p.when.field);
    (p.options_depends && p.options_depends.fields || []).forEach((f) => set.add(f));
  });
  return set;
}

fetch("/api/fetch/sources")
  .then((r) => r.json())
  .then((d) => {
    fetchSpecs = d.items || [];
    const sel = document.getElementById("fetch-source");
    sel.innerHTML = fetchSpecs.map((s) => `<option value="${s.id}">${App.escapeHtml(s.label)}</option>`).join("");
    renderParams();
    sel.addEventListener("change", renderParams);
    document.getElementById("fetch-params").addEventListener("change", (ev) => {
      const spec = currentSpec();
      if (spec && controllerFields(spec).has(ev.target.getAttribute("data-name"))) renderParams();
    });
  })
  .catch(() => App.toast("取数源清单加载失败", "bad"));

document.getElementById("fetch-run").addEventListener("click", () => {
  const spec = currentSpec();
  if (!spec) return;
  const missing = (spec.params || []).filter((p) => {
    const el = document.querySelector(`[data-name="${p.name}"]`);
    return p.required && el && !String(el.value).trim();
  });
  if (missing.length) { App.toast("先填：" + missing.map((p) => p.label).join("、"), "warn"); return; }
  fetch("/api/fetch", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: spec.id, params: collectParams(spec) }),
  })
    .then((r) => r.json())
    .then((d) => {
      if (d.error) { App.toast(d.error, "bad"); return; }
      App.toast("已开始取数，进度见下方", "info");
    })
    .catch((err) => App.toast("启动失败：" + err, "bad"));
});

function resultTable(items) {
  if (!items || !items.length) return "";
  const rows = items.map((it) => `<tr>
    <td>${App.escapeHtml(it.org || "—")}</td>
    <td>${App.escapeHtml(it.title || it.group || "—")}</td>
    <td>${App.escapeHtml(it.date || (it.rank ? "第 " + it.rank + " 名" : "—"))}</td>
    <td>${App.escapeHtml(it.file || (it.analysts ? it.analysts.join("、") : "—"))}</td>
  </tr>`).join("");
  return `<div class="table-card" style="margin-top:14px"><table>
    <thead><tr><th>机构</th><th>标题/行业组</th><th>日期/名次</th><th>文件/分析师</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

App.onTasks.push((tasks) => {
  const area = document.getElementById("fetch-progress");
  if (!area) return;
  const mine = tasks.filter((t) => t.kind === "fetch");
  area.innerHTML = mine.map((t) => App.taskCardHTML(t)).join("");
  const done = mine.filter((t) => t.status === "done").pop();
  const box = document.getElementById("fetch-result");
  if (done && done.result && done.result.items) {
    box.innerHTML = resultTable(done.result.items);
  }
});
