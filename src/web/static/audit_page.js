/* 审校页：后台执行 + 结果摘要 + 报告一览 + 按报告修复向导 */

let lastAuditTid = null;

document.addEventListener("DOMContentLoaded", () => {
  // 模型选择与首页/问答页共用同一个本地记忆
  App.bindModelSelect();
});

/* 统一的 POST：网络错误、非 JSON 响应（典型是服务还是旧版本导致 404）都给人话提示 */
async function postJSON(url, body) {
  let resp, text;
  try {
    resp = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    text = await resp.text();
  } catch (e) {
    throw new Error("网络请求失败：" + e);
  }
  try {
    return JSON.parse(text);
  } catch (_) {
    throw new Error(resp.status === 404
      ? "服务端没有这个接口——正在运行的可能还是旧版本服务，请完全重启网页后再试（记得先关掉旧的 5000 端口进程）"
      : "服务返回异常（HTTP " + resp.status + "），请重启服务后重试");
  }
}

function setLastTime(text) {
  const el = document.getElementById("last-audit-time");
  if (el) el.textContent = "上次审校时间：" + text;
}

function loadLatest() {
  fetch("/api/audit/latest").then((r) => r.json()).then((d) => {
    if (d.error) { setLastTime("暂无"); return; }
    setLastTime(d.ts_pretty || "未知");
    renderSummary(d);
    window.__latestAudit = d;
  }).catch(() => {});
}

function renderSummary(d) {
  const box = document.getElementById("audit-summary");
  const c = d.counts || {};
  const doubts = d.anchor_doubts || [];
  const nDoubt = c["锚点存疑"] || doubts.length || 0;
  box.hidden = false;
  box.innerHTML = `
    <div class="meta-card">
      <div>最新报告：<a href="/outputs/${encodeURIComponent(d.report)}" target="_blank" rel="noopener">${App.escapeHtml(d.report)}</a></div>
      <div>矛盾 ${c["矛盾"] || 0} · 时点差异 ${c["时点差异"] || 0} · 过期 ${c["过期"] || 0} · 未摄入 ${c["未摄入文件"] || 0} 文件 · 重复件 ${c["重复文件"] || 0} · 同名变更 ${c["同名变更"] || 0} · ${c["内容缺口页"] || 0} 页缺口 · 孤立 ${c["孤立"] || 0} · 需复核 ${c["需复核"] || 0} · 格式损坏 ${c["格式损坏"] || 0}</div>
      <div>锚点存疑 <b>${nDoubt}</b> 条 — 答案里的数字在它标注的原文页里找不到，需人工核对${doubts.length ? `（下面列出最近 ${Math.min(doubts.length, 20)} 条）` : ""}</div>
      ${doubts.length ? `<details><summary class="muted tiny" style="cursor:pointer;">展开存疑条目</summary>
        <ul class="muted tiny" style="padding-left:20px;">
          ${doubts.slice(-20).map((x) => `<li>${App.escapeHtml(x.file || "")}（${App.escapeHtml(String(x.loc || ""))}）：${App.escapeHtml(x.reason || String(x.number || ""))}<br><span class="muted">${App.escapeHtml((x.sentence || "").slice(0, 120))}</span></li>`).join("")}
        </ul></details>` : ""}
      <p><button class="ghost" id="ai-review-btn">AI 初判：按严重性排序并说明</button>
        <button class="ghost" id="fix-open">按最新报告修改 wiki</button></p>
      ${d.ai_report ? `<p class="muted" style="margin:0;">AI 初审版：<a href="/outputs/${encodeURIComponent(d.ai_report)}" target="_blank" rel="noopener">${App.escapeHtml(d.ai_report)}</a>（高 ${(d.ai_counts || {})["高"] || 0} · 中 ${(d.ai_counts || {})["中"] || 0} · 低 ${(d.ai_counts || {})["低"] || 0}）AI 判断仅供参考，问题一条没删</p>` : ""}
    </div>`;
  document.getElementById("fix-open").addEventListener("click", () => openWizard(window.__latestAudit));
  document.getElementById("ai-review-btn").addEventListener("click", startAiReview);
}

function startAiReview() {
  const d = window.__latestAudit;
  if (!d) { App.toast("还没有审校结果，请先执行审校", "warn"); return; }
  const btn = document.getElementById("ai-review-btn");
  btn.disabled = true;
  postJSON("/api/audit/ai-review", { ts: d.ts, model: App.model() })
    .then((d2) => {
      if (d2.error) { btn.disabled = false; App.toast(d2.error, "bad"); return; }
      // 按钮保持禁用直到任务结束（onTasks 轮询里恢复），避免重复排队
      App.toast("AI 初审已在后台运行，矛盾多时要等一会儿，可随意切换页面", "info");
    })
    .catch((err) => { btn.disabled = false; App.toast(err.message, "bad"); });
}

let lastAiReviewDone = null;

App.onTasks.push((tasks) => {
  const area = document.getElementById("audit-progress");
  if (area) {
    const mine = tasks.filter((t) => t.kind === "audit" || t.kind === "fix" || t.kind === "ai_review");
    area.innerHTML = mine.map((t) => App.taskCardHTML(t)).join("");
    let refreshed = false;
    const auditDone = mine.find((t) => t.kind === "audit" && t.status === "done" && t.id !== lastAuditTid);
    if (auditDone) {
      lastAuditTid = auditDone.id;
      refreshed = true;
    }
    const fixDone = mine.find((t) => t.kind === "fix" && t.status === "done" && t.id !== lastAuditTid);
    if (fixDone) {
      lastAuditTid = fixDone.id;
      App.toast("修复完成，正在刷新结果", "ok");
      refreshed = true;
    }
    const aiReviewDone = mine.find((t) => t.kind === "ai_review" && t.id !== lastAiReviewDone
                                      && (t.status === "done" || t.status === "error" || t.status === "cancelled"));
    if (aiReviewDone) {
      lastAiReviewDone = aiReviewDone.id;
      if (aiReviewDone.status === "done") {
        App.toast("AI 初审完成，正在刷新结果", "ok");
        refreshed = true;
      } else {
        App.toast("AI 初审未完成：" + (aiReviewDone.error || aiReviewDone.detail || "已取消"), "bad");
      }
    }
    if (refreshed) { loadLatest(); renderReports(); }
    // 初审跑着的时候禁用按钮，防止重复排队
    const btn = document.getElementById("ai-review-btn");
    if (btn) btn.disabled = mine.some((t) => t.kind === "ai_review" && (t.status === "running" || t.status === "waiting"));
  }
});

document.getElementById("audit-btn").addEventListener("click", () => {
  const btn = document.getElementById("audit-btn");
  btn.disabled = true;
  postJSON("/api/audit/run", { model: App.model() })
    .then((d) => {
      if (d.error) { btn.disabled = false; App.toast(d.error, "bad"); return; }
      btn.disabled = false;
      App.toast("审校已在后台运行，可随意切换页面", "info");
    })
    .catch((err) => { btn.disabled = false; App.toast(err.message, "bad"); });
});

/* ---------- 修复向导 ---------- */

function opt(v, text, sel) { return `<option value="${App.escapeHtml(v)}" ${sel ? "selected" : ""}>${App.escapeHtml(text)}</option>`; }

const SEV_CLASS = { "高": "high", "中": "mid", "低": "low" };

/* 向导的动作清单有两个来源：
   - AI 初审版（/api/audit/latest 顺带返回的 ai_items）：矛盾按严重性排好序，每条带 sev/verdict/why —— 有就优先用它
   - 脚本审校原报告：没有 AI 初审（或旧版初审文件没存 items）时的兜底，字段口径与原来一致 */
function collectBase(d) {
  return {
    conflicts: (d.conflicts || []).filter((x) => x.quote_a && x.quote_b),
    review: d.needs_review_pages || [],
    stale: d.stale || [],
    missing: (d.missing_files || []).map((p) => ({ path: p })),
    merges: d.merge_suggestions || [],
  };
}

function collectAi(d) {
  const items = d.ai_items || [];
  return {
    conflicts: items.filter((x) => x.category === "矛盾" && x.quote_a && x.quote_b),
    review: items.filter((x) => x.category === "需复核"),
    stale: items.filter((x) => x.category === "过期"),
    missing: items.filter((x) => x.category === "未摄入文件" && x.path),
    merges: items.filter((x) => x.category === "合并建议"),
  };
}

function conflictHtml(c, i) {
  const sev = SEV_CLASS[c.sev] || "mid";
  const title = `${c.sev ? `<span class="sev-tag ${sev}">${App.escapeHtml(c.sev)}</span>` : ""}页面「${App.escapeHtml(c.page || "")}」` +
    (c.verdict ? ` · ${App.escapeHtml(c.verdict)}` : "");
  const why = c.why ? `<div class="fi-why">AI：${App.escapeHtml(c.why)}</div>` : "";
  return `<div class="fix-item" data-kind="conflict" data-i="${i}">
    <div class="fi-title">${title}</div>
    ${why}
    <div class="fi-quotes">
      <div class="fq">A：「${App.escapeHtml(c.quote_a)}」</div>
      <div class="fq">B：「${App.escapeHtml(c.quote_b)}」</div>
    </div>
    <label><input type="radio" name="cf${i}" value="skip" checked>跳过</label>
    <label><input type="radio" name="cf${i}" value="a">保留 A，删除 B</label>
    <label><input type="radio" name="cf${i}" value="b">保留 B，删除 A</label>
    <label><input type="radio" name="cf${i}" value="both">双方都保留，仅记录标注</label>
  </div>`;
}

function openWizard(d) {
  const body = document.getElementById("fix-body");
  const useAi = Array.isArray(d.ai_items) && d.ai_items.length > 0;
  const { conflicts, review, stale, missing, merges } = useAi ? collectAi(d) : collectBase(d);
  const pages = d.pages || [];
  const parts = [];

  if (conflicts.length) {
    parts.push(`<h3>矛盾（${useAi ? "按 AI 初判严重性排序，" : ""}选择保留哪一方，或仅标注）</h3>`);
    conflicts.forEach((c, i) => parts.push(conflictHtml(c, i)));
  }

  if (review.length) {
    parts.push(`<h3>需复核（删除原文件后保留的无锚点内容，勾选确认后清除标记）</h3>`);
    review.forEach((r) => {
      parts.push(`<div class="fix-item"><label class="fix-check" data-kind="review" data-slug="${App.escapeHtml(r.slug)}">
        <input type="checkbox">${App.escapeHtml(r.title || r.slug || "")}：${App.escapeHtml(r.reason || r.why || "")}</label></div>`);
    });
  }

  if (stale.length) {
    parts.push(`<h3>过期（勾选后只追加「已人工复核」标注，不动正文）</h3>`);
    stale.forEach((s) => {
      const note = (s.why || s.note) ? ` <span class="muted">（${App.escapeHtml(s.why || s.note)}）</span>` : "";
      parts.push(`<div class="fix-item"><label class="fix-check" data-kind="stale" data-slug="${App.escapeHtml(s.slug)}">
        <input type="checkbox">${App.escapeHtml(s.title || s.slug || "")}${note}</label></div>`);
    });
  }

  if (missing.length) {
    parts.push(`<h3>未摄入（勾选后执行摄入任务）</h3>`);
    missing.forEach((p) => {
      parts.push(`<div class="fix-item"><label class="fix-check" data-kind="ingest" data-path="${App.escapeHtml(p.path)}">
        <input type="checkbox" checked>${App.escapeHtml(p.path)}</label></div>`);
    });
  }

  const duplicates = d.duplicate_files || [];
  if (duplicates.length) {
    parts.push(`<h3>重复件（SHA256 与已摄入文件一致，不入库，可人工清理）</h3>`);
    duplicates.forEach((x) => {
      parts.push(`<div class="fix-item"><div class="fi-title">${App.escapeHtml(x.path)} ← ${App.escapeHtml(x.duplicate_of)}</div></div>`);
    });
  }

  const changed = d.changed_files || [];
  if (changed.length) {
    parts.push(`<h3>同名但内容已变化（需确认是否重新摄入）</h3>`);
    changed.forEach((x) => {
      parts.push(`<div class="fix-item"><div class="fi-title">${App.escapeHtml(x.path)}</div></div>`);
    });
  }

  const merges = d.merge_suggestions || [];
  const pages = d.pages || [];
  if (merges.length) {
    parts.push(`<h3>合并建议（选择合并方向，或跳过）</h3>`);
    merges.forEach((m, i) => {
      if (m.kind === "pair") {
        parts.push(`<div class="fix-item" data-kind="pair" data-mi="${i}">
          <div class="fi-title">${App.escapeHtml((m.a || {}).title || "")} ↔ ${App.escapeHtml((m.b || {}).title || "")}：${App.escapeHtml(m.note || m.why || "")}</div>
          <select class="merge-dir">
            ${opt("skip", "跳过", true)}
            ${opt("ab", `「${(m.a || {}).title || ""}」并入「${(m.b || {}).title || ""}」`, false)}
            ${opt("ba", `「${(m.b || {}).title || ""}」并入「${(m.a || {}).title || ""}」`, false)}
          </select>
        </div>`);
      } else {
        const opts = pages.filter((p) => p.slug !== m.slug)
          .map((p) => opt(p.slug, `并入「${p.title}」`, false)).join("");
        parts.push(`<div class="fix-item" data-kind="fragment" data-mi="${i}">
          <div class="fi-title">${App.escapeHtml(m.title || m.slug || "")}：${App.escapeHtml(m.note || m.why || "")}</div>
          <select class="merge-dir">${opt("skip", "跳过", true)}${opts}</select>
        </div>`);
      }
    });
  }

  body.innerHTML = parts.length ? parts.join("")
    : `<p class="muted">最新报告里没有可由程序执行的修复动作。</p>`;

  // 顶部一句话说清选项依据的是哪份报告
  const note = document.querySelector("#fix-modal .modal-note");
  if (note) {
    note.textContent = useAi
      ? `下面的动作来自最新报告的 AI 初判版（${d.ai_report || "audit-ai-" + (d.ts || "") + ".md"}）：矛盾已按严重性排序并附 AI 说明。AI 只供参考，怎么处理仍由你决定；勾选认可的处理方式，确认后才会动 wiki（全程写日志）。`
      : "下面是审校报告中可以由程序执行的动作，先勾选你认可的处理方式，确认后才会动 wiki（全程写日志）。";
  }
  document.getElementById("fix-modal").hidden = false;

  document.getElementById("fix-confirm").onclick = () => {
    const actions = [];
    conflicts.forEach((c, i) => {
      const v = body.querySelector(`input[name="cf${i}"]:checked`);
      if (!v || v.value === "skip") return;
      actions.push({ type: "conflict", page: c.slug, keep: v.value, quote_a: c.quote_a, quote_b: c.quote_b });
    });
    body.querySelectorAll('[data-kind="stale"] input:checked, [data-kind="review"] input:checked').forEach((cb) => {
      actions.push({ type: "mark_reviewed", page: cb.closest("label").dataset.slug });
    });
    body.querySelectorAll('[data-kind="ingest"] input:checked').forEach((cb) => {
      actions.push({ type: "ingest", path: cb.closest("label").dataset.path });
    });
    merges.forEach((m, i) => {
      const kind = m.kind === "pair" ? "pair" : "fragment";
      const el = body.querySelector(`.fix-item[data-kind="${kind}"][data-mi="${i}"]`);
      if (!el) return;
      const v = el.querySelector(".merge-dir") ? el.querySelector(".merge-dir").value : "skip";
      if (v === "skip") return;
      if (m.kind === "pair") {
        actions.push(v === "ab"
          ? { type: "merge", from: m.a.slug, into: m.b.slug }
          : { type: "merge", from: m.b.slug, into: m.a.slug });
      } else if (v) {
        actions.push({ type: "merge", from: m.slug, into: v });
      }
    });
    if (!actions.length) { App.toast("没有勾选任何修复动作", "warn"); return; }
    postJSON("/api/audit/fix", { actions, model: App.model() })
      .then((d2) => {
        if (d2.error) { App.toast(d2.error, "bad"); return; }
        document.getElementById("fix-modal").hidden = true;
        App.toast("修复任务已启动，完成后自动刷新", "info");
      })
      .catch((err) => App.toast(err.message, "bad"));
  };
}

loadLatest();

/* ---------- 报告一览（审校 + AI 初审，动态渲染，新报告自动出现） ---------- */

function sevText(counts) {
  if (!counts || !Object.keys(counts).length) return "";
  // AI 初审版按严重性计数；审校报告按问题类别计数
  if (counts["高"] !== undefined) return `高 ${counts["高"] || 0} · 中 ${counts["中"] || 0} · 低 ${counts["低"] || 0}`;
  const parts = [];
  if (counts["矛盾"]) parts.push("矛盾 " + counts["矛盾"]);
  if (counts["时点差异"]) parts.push("时点差异 " + counts["时点差异"]);
  if (counts["过期"]) parts.push("过期 " + counts["过期"]);
  if (counts["未摄入文件"]) parts.push("未摄入文件 " + counts["未摄入文件"]);
  if (counts["内容缺口页"]) parts.push("缺口 " + counts["内容缺口页"] + " 页");
  if (counts["孤立"]) parts.push("孤立 " + counts["孤立"]);
  if (counts["需复核"]) parts.push("需复核 " + counts["需复核"]);
  if (counts["格式损坏"]) parts.push("格式损坏 " + counts["格式损坏"]);
  if (counts["锚点存疑"]) parts.push("锚点存疑 " + counts["锚点存疑"]);
  return parts.join(" · ") || "（无问题）";
}

function reportRow(x) {
  const badge = x.kind === "ai_review"
    ? `<span class="audit-badge ai">AI 初审</span>`
    : `<span class="audit-badge base">审校</span>`;
  return `<tr>
    <td style="white-space:nowrap;">${App.escapeHtml(x.ts_pretty || x.ts || "")}</td>
    <td>${badge}</td>
    <td class="muted">${App.escapeHtml(sevText(x.counts))}</td>
    <td style="text-align:right;"><a href="/outputs/${encodeURIComponent(x.file)}" target="_blank" rel="noopener">查看报告</a></td>
  </tr>`;
}

function renderReports() {
  const box = document.getElementById("audit-reports");
  if (!box) return;
  fetch("/api/audit/reports", { cache: "no-cache" }).then((r) => r.json()).then((d) => {
    const items = d.items || [];
    if (!items.length) {
      box.innerHTML = `<p class="muted">还没有报告——点上面「执行审校」跑完第一轮后，这里会列出全部报告。</p>`;
      return;
    }
    box.innerHTML = `<div class="meta-card" style="padding:0;overflow:hidden;">
      <table style="margin:0;">
        <thead><tr><th>时间</th><th>类型</th><th>内容摘要</th><th></th></tr></thead>
        <tbody>${items.map(reportRow).join("")}</tbody>
      </table></div>`;
  }).catch(() => { box.innerHTML = `<p class="muted">报告清单加载失败，请刷新页面重试。</p>`; });
}

renderReports();

/* ---------- 建议建页（完整性建议，不算健康问题） ---------- */

function loadPageProposals() {
  fetch("/api/page-proposals/pending", { cache: "no-cache" }).then((r) => r.json()).then((d) => {
    const section = document.getElementById("pp-section");
    const list = document.getElementById("pp-list");
    if (!section || !list) return;
    const items = d.items || [];
    if (!items.length) { section.hidden = true; return; }
    section.hidden = false;
    list.innerHTML = "摄入时发现 " + items.length + " 个被反复提到但还没有知识页的实体：" +
      items.slice(0, 6).map((p) => App.escapeHtml(p.name)).join("、") +
      (items.length > 6 ? " 等" : "") + "。确认后将定向摄入建页（句句带出处）。";
  }).catch(() => {});
}

document.getElementById("pp-open").addEventListener("click", () => {
  fetch("/api/page-proposals/pending", { cache: "no-cache" })
    .then((r) => r.json())
    .then((d) => {
      if ((d.items || []).length) App.openPageProposalModal(d.items);
      else App.toast("建页建议已处理完毕", "info");
    })
    .catch(() => {});
});

App.onPageProposalsChanged = loadPageProposals;
loadPageProposals();
