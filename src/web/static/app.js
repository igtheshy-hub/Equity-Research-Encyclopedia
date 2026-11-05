/* 行业知识库 全局脚本：任务轮询与角标、提取弹窗、原文对照面板、表格过滤 */

/* ---------- 小工具 ---------- */

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

/* ---------- 同源头（问题 1 的 CSRF 防线） ----------
   后端所有"会改状态"的接口都要求 X-Requested-With: wiki。
   浏览器发跨站请求时带不上自定义头，所以这个头能挡住"别的网页偷偷调我的删除/上传接口"。
   跨站表单也发不出这个头，所以删除接口的确认弹窗也绕不过去。 */
(function () {
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      if (url.indexOf("/api/") === 0) {
        init = init || {};
        const headers = new Headers(init.headers || (input && input.headers) || {});
        headers.set("X-Requested-With", "wiki");
        init.headers = headers;
      }
    } catch (_) { /* 包装失败就按原样发 */ }
    return origFetch.call(this, input, init);
  };
})();

window.App = {
  onTasks: [],      // 页面脚本注册的轮询回调 (tasks, active) => void
  /* 合法模型 id 清单（服务端注入）。用来挡住 URL 里带的非法模型名：
     非法值存进 localStorage 会让问答/上传/审校全部 400，网页端直接瘫。 */
  validModel(id) {
    const list = window.MODEL_IDS || [];
    if (!list.length) return true;         // 没注入就不拦（保守）
    return list.indexOf(id) >= 0;
  },
  /* 当前选中的大模型：页面上有 #model-select 就用它，否则读本地记的那个。
     问答、取名、摄入、审校、修复共用同一个选择，全站不用改 config.json。 */
  model() {
    const sel = document.getElementById("model-select");
    if (sel && sel.value) return sel.value;
    const saved = localStorage.getItem("askModel") || "";
    if (saved && !App.validModel(saved)) {
      localStorage.removeItem("askModel");   // 非法值清掉，别把网页打死
      return "";
    }
    return saved;
  },
  /* 把模型选择挂到页面上，并让它与本地记忆同步 */
  bindModelSelect() {
    const sel = document.getElementById("model-select");
    if (!sel) return null;
    const saved = localStorage.getItem("askModel");
    if (saved && Array.from(sel.options).some((o) => o.value === saved)) sel.value = saved;
    sel.addEventListener("change", () => localStorage.setItem("askModel", sel.value));
    return sel;
  },
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
  fetch("/api/tasks", {cache: "no-cache"})
    .then((r) => (r.status === 304 ? null : r.json()))
    .then((d) => { if (!d) return;
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
  } else if (t.status === "cancelled") {
    mid = `<div class="tc-err">已取消${t.detail ? "：" + App.escapeHtml(t.detail) : ""}</div>`;
  } else if (t.status === "done") {
    mid = `<div class="tc-ok">${App.doneText(t)}</div>`;
  }
  const ack = (t.status === "done" || t.status === "error" || t.status === "cancelled")
    ? `<button class="icon-btn tc-ack" data-tid="${t.id}" title="知道了，移除">✕</button>` : "";
  const cancel = (t.status === "running" || t.status === "waiting")
    ? `<button class="icon-btn tc-cancel" data-tid="${t.id}" title="取消任务">取消</button>` : "";
  return `<div class="task-card ${t.status}" data-tid="${t.id}">
    <div class="tc-top"><span class="tc-kind">${App.escapeHtml(t.kind_label || t.kind)}</span>
      <span class="tc-label">${App.escapeHtml(t.label || "")}</span>${cancel}${ack}</div>${mid}</div>`;
};

App.doneText = function (t) {
  const r = t.result;
  try {
    if (t.kind === "classify" && Array.isArray(r)) {
      const bad = r.filter((x) => !x.ok);
      if (bad.length) return `完成，${bad.length} 份未归类：` + bad.map((x) => App.escapeHtml(x.file)).join("、");
      return "全部文件都已取名并进入资料库";
    }
    if (t.kind === "ingest" && Array.isArray(r)) {
      const ok = r.filter((x) => !x.error);
      const bad = r.filter((x) => x.error);
      const parts = [];
      if (ok.length) parts.push("已摄入：" + ok.map((x) => `${App.escapeHtml(x.file)}（${x.entries_count} 条）`).join("、"));
      else parts.push("没有文件产出条目（可能取名未通过、已摄入过或是扫描版）");
      if (bad.length) parts.push("摄入失败：" + bad.map((x) => `${App.escapeHtml(x.file)}（${App.escapeHtml(x.error)}）`).join("、"));
      return parts.join("；");
    }
    if (t.kind === "delete" && Array.isArray(r)) {
      const aborted = r.filter((x) => x.batch_aborted);
      const ok = r.filter((x) => !x.error && !x.batch_aborted);
      const bad = r.filter((x) => x.error && !x.batch_aborted);
      const parts = [];
      if (ok.length) {
        parts.push("已删除：" + ok.map((x) => {
          const pages = (x.pages_deleted || []).length ? `，移除页面 ${x.pages_deleted.map((t) => App.escapeHtml(t)).join("、")}` : "";
          const review = x.needs_review ? `，${x.needs_review} 条转为需复核` : "";
          return `${App.escapeHtml(x.file)}（-${x.statements} 条${pages}${review}）`;
        }).join("、"));
      }
      if (bad.length) parts.push("失败：" + bad.map((x) => `${App.escapeHtml(x.file)}（${App.escapeHtml(x.error)}）`).join("、"));
      if (aborted.length) parts.push("整批已中止，未继续删除后面的文件");
      return parts.join("；") || "没有删除任何文件";
    }
    if (t.kind === "fetch" && r && r.summary) {
      return App.escapeHtml(r.summary);
    }
    if (t.kind === "report" && r && r.file) {
      return `报告已生成：<a href="/outputs/${encodeURIComponent(r.file)}" target="_blank" rel="noopener">${App.escapeHtml(r.file)}</a>`;
    }
    if (t.kind === "report" && r && r.report_job) {
      return `阶段完成，<a href="/report#job=${encodeURIComponent(r.report_job)}">去报告页继续</a>`;
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
  if (ack) {
    fetch("/api/tasks/" + ack.dataset.tid + "/ack", { method: "POST" })
      .then(() => tickTasks())
      .catch(() => {});
    return;
  }
  const cancel = e.target.closest(".tc-cancel");
  if (cancel) {
    fetch("/api/tasks/" + cancel.dataset.tid + "/cancel", { method: "POST" })
      .then((r) => r.json().then((d) => ({ status: r.status, body: d })))
      .then(({ status, body }) => {
        App.toast(status === 200 ? "已请求取消，当前步骤结束后停止" : (body.error || "取消失败"),
                  status === 200 ? "info" : "bad");
        tickTasks();
      })
      .catch(() => {});
  }
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

let pendingProposalItems = [];

App.openExtractModal = function (items, note) {
  const modal = $("#extract-modal");
  const body = $("#extract-body");
  if (!modal || !body || !items.length) return;
  pendingProposalItems = items;
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
        pendingProposalItems = [];
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
/* 稍后处理只推迟提醒；不再提醒才真正把提案标为已处理。两者都不会丢提案本身。 */
function proposalAction(path, okMsg) {
  const items = pendingProposalItems;
  if (!items.length) { closeModal("extract-modal"); return; }
  Promise.all(items.map((it) => fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conv_id: it.conv_id, msg_id: it.msg_id }),
  }))).then(() => {
    pendingProposalItems = [];
    closeModal("extract-modal");
    App.toast(okMsg, "info");
  }).catch((err) => App.toast("操作失败：" + err, "bad"));
}

const extractLater = $("#extract-later");
if (extractLater) extractLater.addEventListener("click", () =>
  proposalAction("/api/proposals/defer", "已稍后处理，24 小时后再提醒"));

const extractDismiss = $("#extract-dismiss");
if (extractDismiss) extractDismiss.addEventListener("click", () =>
  proposalAction("/api/proposals/dismiss", "已不再提醒；提案仍保留在会话里"));

/* 任一页面加载时检查未处理的沉淀提案（只读查询，不会因为刷新页面就被标记为已提醒） */
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
