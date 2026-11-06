/* 原文件页：未摄入筛查、勾选摄入与删除、后台进度 */

const filesRows = JSON.parse(document.getElementById("files-data").textContent || "[]");
const filesByName = Object.fromEntries(filesRows.map((r) => [r.name, r]));
const RELOAD_KEY = "filesReloadedTask";
const myTaskIds = new Set();
let onlyFresh = false;

function checkedNames() {
  return Array.from(document.querySelectorAll(".files-check:checked")).map((c) => c.value);
}

function syncButtons() {
  const names = checkedNames();
  const ingestable = names.filter((n) => {
    const r = filesByName[n];
    return r && !r.ingested_at && !r.duplicate_of;
  });
  const del = document.getElementById("files-delete");
  if (del) {
    del.disabled = names.length === 0;
    del.textContent = names.length ? `删除所选（${names.length}）` : "删除所选";
  }
  const ing = document.getElementById("files-ingest");
  if (ing) {
    ing.disabled = ingestable.length === 0;
    ing.textContent = ingestable.length ? `摄入所选（${ingestable.length}）` : "摄入所选";
  }
  const all = document.getElementById("files-check-all");
  if (all) {
    const visible = Array.from(document.querySelectorAll("tr.row:not([hidden]) .files-check"));
    const checked = visible.filter((c) => c.checked);
    all.checked = visible.length > 0 && checked.length === visible.length;
  }
}

function applyFilter() {
  document.querySelectorAll("tr.row").forEach((tr) => {
    tr.hidden = onlyFresh && tr.dataset.ingested === "1";
  });
  const btn = document.getElementById("files-filter");
  if (btn) btn.textContent = onlyFresh ? "显示全部" : "只看未摄入";
  syncButtons();
}

function openConfirm(names) {
  const body = document.getElementById("del-body");
  const items = names.map((n) => filesByName[n]).filter(Boolean);
  const dup = items.filter((r) => r.duplicate);
  if (dup.length) {
    body.innerHTML = `<p class="banner bad">${dup.map((r) => App.escapeHtml(r.name)).join("、")} 在资料库里有同名多份，无法确定该删哪一份。请先在 raw/ 里改名或移走重复文件。</p>`;
    document.getElementById("del-modal").hidden = false;
    document.getElementById("del-confirm").disabled = true;
    return;
  }
  document.getElementById("del-confirm").disabled = false;
  body.innerHTML = "<ul class=\"del-list\">" + items.map((r) => {
    const bits = [r.kind_label + (r.entries_count ? `，${r.entries_count} 条知识` : "")];
    bits.push(r.statements ? `被 ${r.pages.length} 个页面引用 ${r.statements} 条` : "没有页面引用它的条目");
    const empty = (r.empty_pages || []).length
      ? `<div class="muted">会清空并移除页面：${r.empty_pages.map((t) => App.escapeHtml(t)).join("、")}</div>` : "";
    const miss = r.missing ? `<div class="muted">原文件已不在 raw/，只清理清单与残留</div>` : "";
    const dup = r.duplicate_of
      ? `<div class="muted">与已入库文件 ${App.escapeHtml(r.duplicate_of)} 内容相同，摄入不会新增条目</div>` : "";
    return `<li><b>${App.escapeHtml(r.name)}</b>
      <div class="muted">${App.escapeHtml(bits.join("；"))}</div>${empty}${miss}${dup}</li>`;
  }).join("") + "</ul>";
  document.getElementById("del-modal").hidden = false;
  document.getElementById("del-confirm").onclick = () => {
    fetch("/api/files/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files: names }),
    })
      .then((r) => r.json())
      .then((d) => {
        if (d.error) { App.toast(d.error, "bad"); return; }
        myTaskIds.add(d.task_id);
        document.getElementById("del-modal").hidden = true;
        App.toast(`已开始删除 ${names.length} 份，进度见上方`, "info");
      })
      .catch((err) => App.toast("启动失败：" + err, "bad"));
  };
}

function startIngest(names) {
  const rows = names.map((n) => filesByName[n])
    .filter((r) => r && !r.ingested_at && !r.duplicate_of);
  if (!rows.length) { App.toast("没有可摄入的文件", "warn"); return; }
  fetch("/api/files/ingest", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ files: rows.map((r) => r.name) }),
  })
    .then((r) => r.json())
    .then((d) => {
      if (d.error) { App.toast(d.error, "bad"); return; }
      myTaskIds.add(d.task_id);
      const skip = (d.rejected || []).length;
      App.toast(`已开始摄入 ${d.accepted.length} 份${skip ? `，跳过 ${skip} 份` : ""}，进度见上方`, "info");
      if (skip) {
        App.toast(d.rejected.map((x) => `${x.file}：${x.reason}`).join("；"), "warn");
      }
    })
    .catch((err) => App.toast("启动失败：" + err, "bad"));
}

document.addEventListener("change", (e) => {
  if (e.target.id === "files-check-all") {
    document.querySelectorAll("tr.row:not([hidden]) .files-check")
      .forEach((c) => { c.checked = e.target.checked; });
  }
  if (e.target.classList && (e.target.classList.contains("files-check") || e.target.id === "files-check-all")) {
    syncButtons();
  }
});

document.addEventListener("click", (e) => {
  if (e.target.id === "files-filter") { onlyFresh = !onlyFresh; applyFilter(); return; }
  const one = e.target.closest(".files-del-one");
  if (one) { openConfirm([one.dataset.name]); return; }
  const ing = e.target.closest(".files-ingest-one");
  if (ing) { startIngest([ing.dataset.name]); return; }
  if (e.target.id === "files-delete") {
    const names = checkedNames();
    if (!names.length) { App.toast("先勾选要删除的文件", "warn"); return; }
    openConfirm(names);
    return;
  }
  if (e.target.id === "files-ingest") {
    const names = checkedNames();
    if (!names.length) { App.toast("先勾选要摄入的文件", "warn"); return; }
    startIngest(names);
  }
});

App.onTasks.push((tasks) => {
  const area = document.getElementById("files-progress");
  if (!area) return;
  const mine = tasks.filter((t) => myTaskIds.has(t.id));
  area.innerHTML = mine.map((t) => App.taskCardHTML(t)).join("");
  const done = mine.filter((t) => t.status === "done" || t.status === "error").pop();
  if (done && sessionStorage.getItem(RELOAD_KEY) !== done.id) {
    sessionStorage.setItem(RELOAD_KEY, done.id);  // 记在本地，刷新后不再重复触发
    setTimeout(() => location.reload(), 1600);
  }
});

applyFilter();
