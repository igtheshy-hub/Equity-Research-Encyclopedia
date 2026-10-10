/* 审校页：后台执行 + 结果摘要 + 按报告修复向导 */

let lastAuditTid = null;

function loadLatest() {
  fetch("/api/audit/latest").then((r) => r.json()).then((d) => {
    if (d.error) return;
    renderSummary(d);
    window.__latestAudit = d;
  }).catch(() => {});
}

function renderSummary(d) {
  const box = document.getElementById("audit-summary");
  const c = d.counts || {};
  box.hidden = false;
  box.innerHTML = `
    <div class="meta-card">
      <div>最新报告：<a href="/outputs/${encodeURIComponent(d.report)}" target="_blank" rel="noopener">${App.escapeHtml(d.report)}</a></div>
      <div>矛盾 ${c["矛盾"] || 0} · 时点差异 ${c["时点差异"] || 0} · 过期 ${c["过期"] || 0} · 未摄入 ${c["未摄入文件"] || 0} 文件 / ${c["内容缺口页"] || 0} 页缺口 · 孤立 ${c["孤立"] || 0}</div>
      <p><button class="ghost" id="fix-open">按最新报告修改 wiki</button></p>
    </div>`;
  document.getElementById("fix-open").addEventListener("click", () => openWizard(window.__latestAudit));
}

App.onTasks.push((tasks) => {
  const area = document.getElementById("audit-progress");
  if (!area) return;
  const mine = tasks.filter((t) => t.kind === "audit" || t.kind === "fix");
  area.innerHTML = mine.map((t) => App.taskCardHTML(t)).join("");
  const auditDone = mine.find((t) => t.kind === "audit" && t.status === "done" && t.id !== lastAuditTid);
  if (auditDone) {
    lastAuditTid = auditDone.id;
    loadLatest();
  }
  const fixDone = mine.find((t) => t.kind === "fix" && t.status === "done" && t.id !== lastAuditTid);
  if (fixDone) {
    lastAuditTid = fixDone.id;
    App.toast("修复完成，正在刷新结果", "ok");
    loadLatest();
  }
});

document.getElementById("audit-btn").addEventListener("click", () => {
  const btn = document.getElementById("audit-btn");
  btn.disabled = true;
  fetch("/api/audit/run", { method: "POST" })
    .then((r) => r.json())
    .then((d) => {
      if (d.error) { btn.disabled = false; App.toast(d.error, "bad"); return; }
      btn.disabled = false;
      App.toast("审校已在后台运行，可随意切换页面", "info");
    })
    .catch((err) => { btn.disabled = false; App.toast("启动失败：" + err, "bad"); });
});

/* ---------- 修复向导 ---------- */

function opt(v, text, sel) { return `<option value="${App.escapeHtml(v)}" ${sel ? "selected" : ""}>${App.escapeHtml(text)}</option>`; }

function openWizard(d) {
  const body = document.getElementById("fix-body");
  const parts = [];

  const conflicts = (d.conflicts || []).filter((x) => x.quote_a && x.quote_b);
  if (conflicts.length) {
    parts.push(`<h3>矛盾（选择保留哪一方，或仅标注）</h3>`);
    conflicts.forEach((c, i) => {
      parts.push(`<div class="fix-item" data-kind="conflict" data-i="${i}">
        <div class="fi-title">页面「${App.escapeHtml(c.page)}」：${App.escapeHtml(c.note || "")}</div>
        <div class="fi-quotes">
          <div class="fq">A：「${App.escapeHtml(c.quote_a)}」</div>
          <div class="fq">B：「${App.escapeHtml(c.quote_b)}」</div>
        </div>
        <label><input type="radio" name="cf${i}" value="skip" checked>跳过</label>
        <label><input type="radio" name="cf${i}" value="a">保留 A，删除 B</label>
        <label><input type="radio" name="cf${i}" value="b">保留 B，删除 A</label>
        <label><input type="radio" name="cf${i}" value="both">双方都保留，仅记录标注</label>
      </div>`);
    });
  }

  const stale = d.stale || [];
  if (stale.length) {
    parts.push(`<h3>过期（勾选后只追加「已人工复核」标注，不动正文）</h3>`);
    stale.forEach((s) => {
      parts.push(`<div class="fix-item"><label class="fix-check" data-kind="stale" data-slug="${App.escapeHtml(s.slug)}">
        <input type="checkbox">${App.escapeHtml(s.title)}</label></div>`);
    });
  }

  const missing = d.missing_files || [];
  if (missing.length) {
    parts.push(`<h3>未摄入（勾选后执行摄入任务）</h3>`);
    missing.forEach((p) => {
      parts.push(`<div class="fix-item"><label class="fix-check" data-kind="ingest" data-path="${App.escapeHtml(p)}">
        <input type="checkbox" checked>${App.escapeHtml(p)}</label></div>`);
    });
  }

  const merges = d.merge_suggestions || [];
  const pages = d.pages || [];
  if (merges.length) {
    parts.push(`<h3>合并建议（选择合并方向，或跳过）</h3>`);
    merges.forEach((m, i) => {
      if (m.kind === "pair") {
        parts.push(`<div class="fix-item" data-kind="pair" data-mi="${i}">
          <div class="fi-title">${App.escapeHtml(m.a.title)} ↔ ${App.escapeHtml(m.b.title)}：${App.escapeHtml(m.note)}</div>
          <select class="merge-dir">
            ${opt("skip", "跳过", true)}
            ${opt("ab", `「${m.a.title}」并入「${m.b.title}」`, false)}
            ${opt("ba", `「${m.b.title}」并入「${m.a.title}」`, false)}
          </select>
        </div>`);
      } else {
        const opts = pages.filter((p) => p.slug !== m.slug)
          .map((p) => opt(p.slug, `并入「${p.title}」`, false)).join("");
        parts.push(`<div class="fix-item" data-kind="fragment" data-mi="${i}">
          <div class="fi-title">${App.escapeHtml(m.title)}：${App.escapeHtml(m.note)}</div>
          <select class="merge-dir">${opt("skip", "跳过", true)}${opts}</select>
        </div>`);
      }
    });
  }

  if (!parts.length) {
    body.innerHTML = `<p class="muted">最新报告里没有可由程序执行的修复动作。</p>`;
  } else {
    body.innerHTML = parts.join("");
  }
  document.getElementById("fix-modal").hidden = false;

  document.getElementById("fix-confirm").onclick = () => {
    const actions = [];
    conflicts.forEach((c, i) => {
      const v = body.querySelector(`input[name="cf${i}"]:checked`);
      if (!v || v.value === "skip") return;
      actions.push({ type: "conflict", page: c.slug, keep: v.value, quote_a: c.quote_a, quote_b: c.quote_b });
    });
    body.querySelectorAll('[data-kind="stale"] input:checked').forEach((cb) => {
      actions.push({ type: "mark_reviewed", page: cb.closest("label").dataset.slug });
    });
    body.querySelectorAll('[data-kind="ingest"] input:checked').forEach((cb) => {
      actions.push({ type: "ingest", path: cb.closest("label").dataset.path });
    });
    merges.forEach((m, i) => {
      const el = body.querySelector(`.fix-item[data-kind="${m.kind}"][data-mi="${i}"]`);
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
    fetch("/api/audit/fix", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actions }),
    })
      .then((r) => r.json())
      .then((d2) => {
        if (d2.error) { App.toast(d2.error, "bad"); return; }
        document.getElementById("fix-modal").hidden = true;
        App.toast("修复任务已启动，完成后自动刷新", "info");
      })
      .catch((err) => App.toast("启动失败：" + err, "bad"));
  };
}

loadLatest();
