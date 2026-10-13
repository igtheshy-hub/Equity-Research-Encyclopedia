/* 行业知识库 全局脚本：任务轮询与角标、提取弹窗、原文对照面板、表格过滤 */

/* ---------- 小工具 ---------- */

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

window.App = {
  onTasks: [],      // 页面脚本注册的轮询回调 (tasks, active) => void
  escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  },
  toast(msg, type) {
    const wrap = $("#toast-wrap");
    if (!wrap) return;
    const el = document.createElement("div");
    el.className = "toast " + (type || "info");
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(() => { el.classList.add("fade"); }, 2600);
    setTimeout(() => { el.remove(); }, 3200);
  },
};

/* ---------- 任务轮询 ---------- */

function tickTasks() {
  fetch("/api/tasks")
    .then((r) => r.json())
    .then((d) => {
      const tasks = d.tasks || [];
      const active = d.active || {};
      const running = Object.values(active).reduce((a, b) => a + b, 0);
      const badge = $("#task-badge");
      if (badge) {
        badge.hidden = running === 0;
        badge.textContent = running;
        const names = Object.keys(active).filter((k) => active[k] > 0);
        badge.title = names.length ? "正在运行：" + names.join("、") : "";
      }
      App.onTasks.forEach((fn) => { try { fn(tasks, active); } catch (e) { console.error(e); } });
    })
    .catch(() => {});
}

setInterval(() => { if (!document.hidden) tickTasks(); }, 2000);
tickTasks();

/* 任务卡渲染（首页进度区 / 产出页 / 审校页共用） */
App.taskCardHTML = function (t) {
  const pct = Math.max(0, Math.min(100, t.percent || 0));
  let mid = "";
  if (t.status === "running") {
    mid = `<div class="bar"><i style="width:${pct}%"></i></div>
           <div class="tc-detail">${App.escapeHtml(t.detail || (pct ? pct + "%" : "进行中"))}</div>`;
  } else if (t.status === "waiting") {
    mid = `<div class="bar waiting"><i style="width:${pct}%"></i></div><div class="tc-detail">排队等待中（同类任务进行中）</div>`;
  } else if (t.status === "error") {
    mid = `<div class="tc-err">失败：${App.escapeHtml(t.error || "未知错误")}</div>`;
  } else if (t.status === "done") {
    mid = `<div class="tc-ok">${App.doneText(t)}</div>`;
  }
  const ack = (t.status === "done" || t.status === "error")
    ? `<button class="icon-btn tc-ack" data-tid="${t.id}" title="知道了，移除">✕</button>` : "";
  return `<div class="task-card ${t.status}" data-tid="${t.id}">
    <div class="tc-top"><span class="tc-kind">${App.escapeHtml(t.kind_label || t.kind)}</span>
      <span class="tc-label">${App.escapeHtml(t.label || "")}</span>${ack}</div>${mid}</div>`;
};

App.doneText = function (t) {
  const r = t.result;
  try {
    if (t.kind === "classify" && Array.isArray(r)) {
      const bad = r.filter((x) => !x.ok);
      if (bad.length) return `完成，${bad.length} 份未归类：` + bad.map((x) => x.file).join("、");
      return "全部文件都已取名并进入资料库";
    }
    if (t.kind === "ingest" && Array.isArray(r)) {
      const ok = r.filter((x) => !x.error);
      const bad = r.filter((x) => x.error);
      const parts = [];
      if (ok.length) parts.push("已摄入：" + ok.map((x) => `${x.file}（${x.entries_count} 条）`).join("、"));
      else parts.push("没有文件产出条目（可能取名未通过、已摄入过或是扫描版）");
      if (bad.length) parts.push("摄入失败：" + bad.map((x) => `${x.file}（${x.error}）`).join("、"));
      return parts.join("；");
    }
    if (t.kind === "delete" && Array.isArray(r)) {
      const ok = r.filter((x) => !x.error);
      const bad = r.filter((x) => x.error);
      const parts = [];
      if (ok.length) {
        parts.push("已删除：" + ok.map((x) => {
          const pages = (x.pages_deleted || []).length ? `，移除页面 ${x.pages_deleted.join("、")}` : "";
          return `${x.file}（-${x.statements} 条${pages}）`;
        }).join("、"));
      }
      if (bad.length) parts.push("失败：" + bad.map((x) => `${x.file}（${x.error}）`).join("、"));
      return parts.join("；");
    }
    if (t.kind === "report" && r && r.file) {
      return `报告已生成：<a href="/outputs/${encodeURIComponent(r.file)}" target="_blank" rel="noopener">${App.escapeHtml(r.file)}</a>`;
    }
    if (t.kind === "audit" && r && r.counts) {
      const c = r.counts;
      return `完成：矛盾 ${c["矛盾"] || 0}、过期 ${c["过期"] || 0}、未摄入 ${c["未摄入文件"] || 0} 文件、孤立 ${c["孤立"] || 0}`;
    }
    if (t.kind === "fix" && r && Array.isArray(r.notes)) {
      return "修复完成：" + r.notes.slice(0, 3).join("；");
    }
  } catch (e) { /* 结果展示失败不影响主流程 */ }
  return "完成";
};

document.addEventListener("click", (e) => {
  const ack = e.target.closest(".tc-ack");
  if (!ack) return;
  fetch("/api/tasks/" + ack.dataset.tid + "/ack", { method: "POST" })
    .then(() => tickTasks())
    .catch(() => {});
});

/* ---------- 提取（沉淀）弹窗 ---------- */

function extractItemHTML(item, gi) {
  const p = item.proposal;
  const target = p.action === "merge"
    ? `并入已有页「${App.escapeHtml(p.target_title || "")}」` : `新建${p.type === "company" ? "公司" : p.type === "industry" ? "行业" : "概念"}页`;
  const lines = (p.lines || []).map((ln, i) => `
    <label class="extract-line"><input type="checkbox" checked data-g="${gi}" data-i="${i}">
      <span>${App.escapeHtml((p.lines_pretty || p.lines)[i] || ln)}</span></label>`).join("");
  return `<div class="extract-group" data-g="${gi}">
    <div class="eg-head"><b>${App.escapeHtml(p.title)}</b><span class="muted">${target}</span></div>
    ${p.summary ? `<div class="muted eg-summary">${App.escapeHtml(p.summary)}</div>` : ""}
    <div class="eg-lines">${lines}</div>
  </div>`;
}

App.openExtractModal = function (items, note) {
  const modal = $("#extract-modal");
  const body = $("#extract-body");
  if (!modal || !body || !items.length) return;
  $("#extract-note").textContent = note || "";
  body.innerHTML = items.map(extractItemHTML).join("");
  modal.hidden = false;
  $("#extract-confirm").onclick = () => {
    const applies = [];
    items.forEach((item, gi) => {
      const g = body.querySelector(`.extract-group[data-g="${gi}"]`);
      const checked = $$("input:checked", g);
      if (!checked.length) return;
      const lines = checked.map((c) => item.proposal.lines[+c.dataset.i]);
      applies.push({ conv_id: item.conv_id, msg_id: item.msg_id, lines });
    });
    if (!applies.length) { closeModal("extract-modal"); return; }
    fetch("/api/distill/apply", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ applies }),
    })
      .then((r) => r.json())
      .then((d) => {
        const ok = (d.results || []).filter((x) => x.ok).length;
        const bad = (d.results || []).filter((x) => !x.ok);
        closeModal("extract-modal");
        if (bad.length) App.toast(`提取完成 ${ok} 项，失败 ${bad.length} 项`, "warn");
        else App.toast(`已提取 ${ok} 个提案进知识库，详情见日志页`, "ok");
        if (App.onDistillApplied) App.onDistillApplied();
      })
      .catch((err) => App.toast("提取失败：" + err, "bad"));
  };
};

function closeModal(id) {
  const m = document.getElementById(id);
  if (m) m.hidden = true;
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-close]");
  if (btn) closeModal(btn.dataset.close);
  if (e.target.classList && e.target.classList.contains("modal-backdrop")) {
    closeModal(e.target.id);
  }
});
const extractLater = $("#extract-later");
if (extractLater) extractLater.addEventListener("click", () => closeModal("extract-modal"));

/* 任一页面加载时检查未处理的沉淀提案（服务端记 presented，只弹一次） */
fetch("/api/proposals/pending")
  .then((r) => r.json())
  .then((d) => {
    const items = d.items || [];
    if (items.length) {
      const titles = [...new Set(items.map((x) => x.conv_title || ""))].filter(Boolean);
      App.openExtractModal(items, "来自会话「" + (titles.join("」「") || "未命名") + "」的可沉淀新知识");
    }
  })
  .catch(() => {});

/* ---------- 角标点击：加 hl 参数；wiki 页放右栏 iframe ---------- */

function enrichCiteLinks(root) {
  $$("sup.cite a", root).forEach((a) => {
    if (a.dataset.enriched) return;
    a.dataset.enriched = "1";
    const block = a.closest("li, p, td");
    if (!block) return;
    const text = block.textContent.replace(a.textContent, "").replace(/\s+/g, "").slice(0, 30);
    if (text) a.href += "&hl=" + encodeURIComponent(text);
  });
}

App.enrichCiteLinks = enrichCiteLinks;
document.addEventListener("DOMContentLoaded", () => enrichCiteLinks(document));

document.addEventListener("click", (e) => {
  const a = e.target.closest("sup.cite a");
  if (!a) return;
  const frame = $("#viewer-frame");
  if (!frame) return; // 无对照栏：保持新标签打开
  e.preventDefault();
  const panel = $("#viewer-panel"), divider = $("#hdivider");
  if (panel) panel.hidden = false;
  if (divider) divider.hidden = false;
  frame.src = a.href;
  const title = $("#viewer-title");
  if (title) title.textContent = a.textContent;
  const saved = localStorage.getItem("pageViewerWidth");
  if (saved) applyPaneWidth(saved);
});

function applyPaneWidth(px) {
  const main = $(".page-main");
  const wrap = $(".page-wrap");
  if (!main || !wrap) return;
  const pct = Math.max(30, Math.min(85, (px / wrap.clientWidth) * 100));
  main.style.flex = `0 0 ${pct}%`;
}

(function initDivider() {
  const divider = $("#hdivider");
  if (!divider) return;
  const saved = localStorage.getItem("pageViewerWidth");
  if (saved && !$("#viewer-panel").hidden) applyPaneWidth(+saved);
  let dragging = false;
  divider.addEventListener("mousedown", (e) => { dragging = true; e.preventDefault(); });
  document.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const wrap = $(".page-wrap");
    const px = Math.max(300, Math.min(wrap.clientWidth - 320, e.clientX - wrap.getBoundingClientRect().left));
    applyPaneWidth(px);
  });
  document.addEventListener("mouseup", (e) => {
    if (!dragging) return;
    dragging = false;
    const wrap = $(".page-wrap");
    if (wrap) localStorage.setItem("pageViewerWidth", String(e.clientX - wrap.getBoundingClientRect().left));
  });
})();

document.addEventListener("click", (e) => {
  if (e.target.id !== "viewer-close") return;
  const frame = $("#viewer-frame");
  if (frame) { frame.src = "about:blank"; }
  const panel = $("#viewer-panel"), divider = $("#hdivider");
  if (panel) panel.hidden = true;
  if (divider) divider.hidden = true;
});

/* ---------- 表格过滤（知识库 / 日志共用） ---------- */

const filter = $("#filter");
if (filter) {
  filter.addEventListener("input", () => {
    const q = filter.value.trim().toLowerCase();
    let shown = 0;
    $$("tr.row").forEach((tr) => {
      const hit = !q || (tr.dataset.search || "").toLowerCase().includes(q);
      tr.style.display = hit ? "" : "none";
      if (hit) shown += 1;
    });
    const count = $("#filter-count");
    if (count) count.textContent = q ? `匹配 ${shown} 行` : "";
  });
}
