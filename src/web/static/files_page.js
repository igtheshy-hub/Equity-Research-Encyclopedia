/* 原文件页：勾选、删除确认弹窗、后台删除进度 */

const filesRows = JSON.parse(document.getElementById("files-data").textContent || "[]");
const filesByName = Object.fromEntries(filesRows.map((r) => [r.name, r]));
const RELOAD_KEY = "filesReloadedTask";

function checkedNames() {
  return Array.from(document.querySelectorAll(".files-check:checked")).map((c) => c.value);
}

function syncButtons() {
  const n = checkedNames().length;
  const total = document.querySelectorAll(".files-check").length;
  const btn = document.getElementById("files-delete");
  if (btn) {
    btn.disabled = n === 0;
    btn.textContent = n ? `删除所选（${n}）` : "删除所选";
  }
  const all = document.getElementById("files-check-all");
  if (all) all.checked = n > 0 && n === total;
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
    return `<li><b>${App.escapeHtml(r.name)}</b>
      <div class="muted">${App.escapeHtml(bits.join("；"))}</div>${empty}${miss}</li>`;
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
        document.getElementById("del-modal").hidden = true;
        App.toast(`已开始删除 ${names.length} 份，进度见上方`, "info");
      })
      .catch((err) => App.toast("启动失败：" + err, "bad"));
  };
}

document.addEventListener("change", (e) => {
  if (e.target.id === "files-check-all") {
    document.querySelectorAll(".files-check").forEach((c) => { c.checked = e.target.checked; });
  }
  if (e.target.classList && (e.target.classList.contains("files-check") || e.target.id === "files-check-all")) {
    syncButtons();
  }
});

document.addEventListener("click", (e) => {
  const one = e.target.closest(".files-del-one");
  if (one) { openConfirm([one.dataset.name]); return; }
  if (e.target.id === "files-delete") {
    const names = checkedNames();
    if (!names.length) { App.toast("先勾选要删除的文件", "warn"); return; }
    openConfirm(names);
  }
});

App.onTasks.push((tasks) => {
  const area = document.getElementById("files-progress");
  if (!area) return;
  const mine = tasks.filter((t) => t.kind === "delete");
  area.innerHTML = mine.map((t) => App.taskCardHTML(t)).join("");
  const done = mine.filter((t) => t.status === "done" || t.status === "error").pop();
  if (done && sessionStorage.getItem(RELOAD_KEY) !== done.id) {
    sessionStorage.setItem(RELOAD_KEY, done.id);  // 记在本地，刷新后不再重复触发
    setTimeout(() => location.reload(), 1600);
  }
});

syncButtons();
