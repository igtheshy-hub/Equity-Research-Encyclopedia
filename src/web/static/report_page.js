/* 报告页：行业研报任务、大纲确认、缺口勾选、进度与 A-F 沉淀候选 */

const REPORT_RUNNING = new Set(["outlining", "fetching", "writing", "charting", "reviewing", "docxing"]);
const CLASS_LABELS = { A: "综合判断", D: "前瞻预测", E: "推导计算" };

let currentJobId = null;
let currentJob = null;
let outlineLoading = false;
let SOURCE_LABELS = {};

/* 取数源 id → 中文名，缺口清单里显示中文，查不到回落原 id */
function loadSourceLabels() {
  fetch("/api/fetch/sources")
    .then((r) => r.json())
    .then((d) => {
      const map = {};
      (d.items || []).forEach((s) => { map[s.id] = s.label || s.id; });
      SOURCE_LABELS = map;
      if (currentJob && currentJobId) loadJob(currentJobId);
    })
    .catch(() => {});
}

function sourceLabel(id) { return SOURCE_LABELS[id] || id; }

function esc(s) { return App.escapeHtml(s); }

function baseName(p) { return String(p || "").split("/").pop(); }

function hashJobId() {
  const m = /[#&]job=([0-9a-z]+)/i.exec(location.hash || "");
  return m ? m[1] : "";
}

function selectJob(id) {
  currentJobId = id;
  currentJob = null;
  history.replaceState(null, "", "/report#job=" + encodeURIComponent(id));
  document.getElementById("job-detail").innerHTML = '<p class="muted">加载中…</p>';
  loadJob(id);
}

function loadJobs(preferId) {
  return fetch("/api/report/jobs")
    .then((r) => r.json())
    .then((d) => {
      const items = d.items || [];
      const sel = document.getElementById("job-select");
      const box = document.getElementById("job-detail");
      if (!items.length) {
        sel.innerHTML = "";
        sel.hidden = true;
        currentJobId = null;
        currentJob = null;
        document.getElementById("job-tasks").innerHTML = "";
        box.innerHTML = '<p class="muted">还没有研报任务。到 <a href="/ask">问答页</a> 提「帮我写一份XX行业研报」或「写一份XX公司研究报告」即可。</p>';
        return;
      }
      sel.hidden = false;
      sel.innerHTML = items.map((j) => {
        const state = j.status_label || j.status;
        const kind = j.kind_label || "行业研报";
        return `<option value="${j.id}">[${esc(kind)}] ${esc(j.title)}（${esc(state)}）</option>`;
      }).join("");
      const want = preferId || hashJobId() || currentJobId;
      const hit = items.find((j) => j.id === want) || items[0];
      sel.value = hit.id;
      if (hit.id !== currentJobId || !currentJob) selectJob(hit.id);
      else updateSelectLabel(hit);
    })
    .catch(() => App.toast("报告任务列表加载失败", "bad"));
}

function updateSelectLabel(job) {
  const opt = document.querySelector(`#job-select option[value="${job.id}"]`);
  if (opt) opt.textContent = `[${job.kind_label || "行业研报"}] ${job.title}（${job.status_label || job.status}）`;
}

function loadJob(id) {
  return fetch("/api/report/" + encodeURIComponent(id))
    .then((r) => r.json())
    .then((job) => {
      if (!job.id) { App.toast(job.error || "报告任务不存在", "bad"); return; }
      if (job.id !== currentJobId) return;
      currentJob = job;
      updateSelectLabel(job);
      document.getElementById("job-detail").innerHTML = renderDetail(job);
    })
    .catch(() => {});
}

/* ---------- 渲染 ---------- */

function renderDetail(job) {
  const parts = [];
  const status = job.status || "";
  parts.push(`<div class="report-card">
    <div class="report-head">
      <div>
        <div class="report-title">${esc(job.title)}</div>
        <div class="muted">${esc(job.kind_label || "行业研报")} · ${esc(job.question || "")}</div>
      </div>
      <span class="job-status ${status === "error" ? "bad" : status === "done" ? "ok" : "run"}">${esc(job.status_label || status)}</span>
    </div>
    ${linksHTML(job)}
    ${REPORT_RUNNING.has(status) ? "" : `<div class="report-actions"><button class="ghost" data-act="delete-job">删除这份研报</button></div>`}
    ${status === "error" && job.error ? `<div class="warn-strip">${esc(job.error)}</div>
      <div class="report-actions">
        <button class="primary" data-act="continue">继续此阶段</button>
        <button class="ghost" data-act="fallback">改走内置底稿</button>
      </div>` : ""}
  </div>`);

  parts.push(outlineHTML(job));
  parts.push(gapsHTML(job));
  parts.push(candidatesHTML(job));
  parts.push(reviewHTML(job));
  parts.push(logHTML(job));
  return parts.join("");
}

function linksHTML(job) {
  const bits = [];
  if (job.outline_path) {
    bits.push(`<a href="/outputs/${encodeURIComponent(baseName(job.outline_path))}" target="_blank" rel="noopener">大纲 md</a>`);
  }
  if (job.report_path) {
    bits.push(`<a href="/outputs/${encodeURIComponent(baseName(job.report_path))}" target="_blank" rel="noopener">报告 md</a>`);
  }
  if (job.docx_path) {
    bits.push(`<a href="/outputs/download/${encodeURIComponent(baseName(job.docx_path))}">下载 Word</a>`);
  }
  return bits.length ? `<div class="report-links">${bits.join("")}</div>` : "";
}

function outlineHTML(job) {
  if (job.status === "outline_pending") {
    return `<div class="report-card">
      <h2>大纲确认</h2>
      <p class="muted">可以直接改大纲。确认后按这一版成文；改过的话会重新列数据缺口。</p>
      <textarea id="outline-text" rows="18" class="outline-text">${esc(job.outline || "")}</textarea>
      <div class="report-actions">
        <button class="primary" data-act="outline-confirm">确认大纲</button>
      </div>
    </div>`;
  }
  if (!job.outline) return "";
  return `<details class="report-card"><summary>大纲</summary>
    <pre class="report-pre">${esc(job.outline)}</pre></details>`;
}

function gapStateText(g) {
  return { done: "已补", failed: "补数失败", skipped: "已跳过", selected: "已选" }[g.status] || "";
}

function gapsHTML(job) {
  const gaps = job.gaps || [];
  if (!gaps.length) return "";
  if (job.status === "gaps_pending") {
    const hasRequired = gaps.some((g) => g.required);
    const rows = gaps.map((g) => {
      const can = !!(g.fetch && g.fetch.source);
      const required = !!g.required;
      const src = can ? sourceLabel(g.fetch.source) : "无免登录源";
      return `<label class="gap-line">
        <input type="checkbox" value="${esc(g.id)}"${can ? " checked" : ""}${(!can || required) ? " disabled" : ""}>
        <span class="gap-main">${required ? '<span class="tag must">必选</span>' : ""}<b>${esc(g.item)}</b>${g.why ? `<span class="muted">（${esc(g.why)}）</span>` : ""}</span>
        <span class="muted">${esc(src)}</span>
      </label>`;
    }).join("");
    return `<div class="report-card">
      <h2>数据缺口</h2>
      <p class="muted">勾上的缺口会起取数任务补进 raw，补完自动开始成文；没勾的跳过，报告里留数据缺失。公司研报的必选缺口是最近一年内的年报、半年报、季报，缺哪类补哪类，未补齐不能成文；港股季报不强制。</p>
      ${rows}
      <div class="report-actions">
        <button class="primary" data-act="gaps-submit">补选中的缺口并成文</button>
        ${hasRequired ? "" : '<button class="ghost" data-act="gaps-skip">全部跳过，直接成文</button>'}
      </div>
    </div>`;
  }
  const rows = gaps.map((g) => {
    const state = gapStateText(g);
    return `<li>${esc(g.item)}${state ? `<span class="muted">（${esc(state)}）</span>` : ""}</li>`;
  }).join("");
  return `<details class="report-card"><summary>数据缺口（${gaps.length} 条）</summary>
    <ul class="plain-list">${rows}</ul></details>`;
}

function candidatesHTML(job) {
  const cands = job.candidates || [];
  if (!cands.length) return "";
  const pending = job.status === "candidates_pending";
  const rows = cands.map((c) => {
    const checked = c.chosen ? " checked disabled" : "";
    const extra = [];
    if (c.class === "D" && c.condition) extra.push(`预测条件：${esc(c.condition)}`);
    if (c.class === "E" && c.calc) extra.push(`计算口径：${esc(c.calc)}`);
    const detail = extra.length ? `<span class="cand-extra muted">${extra.join("；")}</span>` : "";
    const target = c.target && c.target.title ? `→ ${esc(c.target.title)}` : "";
    return `<label class="cand-line">
      <input type="checkbox" value="${esc(c.id)}"${checked}>
      <span class="cand-badge cls-${esc(c.class)}">${esc(CLASS_LABELS[c.class] || c.class)}</span>
      <span class="cand-text"><span>${esc(c.distill_line || c.source_sentence || "")}</span>
        <span class="muted">${target}${c.chosen ? " · 已写入" : ""}</span>${detail}</span>
    </label>`;
  }).join("");
  const action = pending
    ? `<div class="report-actions"><button class="primary" data-act="cand-apply">写入选中的沉淀</button></div>` : "";
  return `<div class="report-card">
    <h2>沉淀候选（${cands.length} 条）</h2>
    <p class="muted">A 跨材料综合判断、D 前瞻预测写成【个人观点】，E 推导计算写成带口径的数据行。不勾的不进 wiki。</p>
    ${rows}${action}
  </div>`;
}

function reviewHTML(job) {
  const rev = job.review || {};
  const note = rev.sample_note || "";
  const missingRows = (job.missing_data || []).map((m) => `<li class="finding">
    <div class="f-line"><span class="f-tag">数据缺失</span>${esc(m.item || "")}</div>
    <div class="f-note">${m.skipped ? "跳过未取数" : esc(m.error || "取数失败")}，需手动补充</div>
  </li>`).join("");
  const missingBlock = missingRows
    ? `<h3>数据缺失（${(job.missing_data || []).length} 项，需手动补充）</h3><ul class="plain-list">${missingRows}</ul>` : "";
  if (Array.isArray(rev.findings)) {
    const groups = [
      { key: "已修复", title: "已修复" },
      { key: "误报", title: "误报排除" },
      { key: null, title: "遗留（附原因，待人工）" },
    ];
    const blocks = groups.map((g) => {
      const rows = rev.findings.filter((f) => f.where !== "数据缺口"
        && (g.key ? f.outcome === g.key : f.outcome !== "已修复" && f.outcome !== "误报"));
      if (!rows.length) return "";
      const items = rows.map((f) => `<li class="finding">
        <div class="f-line"><span class="f-tag">${esc(f.kind || "")}·${esc(f.level || "")}</span>${esc(f.where || "")}：${esc(f.problem || "")}</div>
        ${f.note ? `<div class="f-note">处置：${esc(f.note)}</div>` : ""}
      </li>`).join("");
      return `<h3>${g.title}（${rows.length}）</h3><ul class="plain-list">${items}</ul>`;
    }).join("");
    if (!blocks && !note && !missingRows) return "";
    return `<details class="report-card"><summary>${esc(rev.summary || "审校记录")}</summary>
      ${note ? `<h3>样板覆盖提示</h3><p>${esc(note)}</p>` : ""}
      ${missingBlock}
      ${blocks}
    </details>`;
  }
  const mech = rev.mechanical || [];
  const issues = rev.issues || [];
  if (!mech.length && !issues.length && !note && !missingRows) return "";
  const mechRows = mech.map((m) => `<li>${esc(m)}</li>`).join("");
  const issueRows = issues.map((i) => `<li><b>${esc(i.level || "")}</b> ${esc(i.where || "")}：${esc(i.problem || "")}</li>`).join("");
  return `<details class="report-card"><summary>审校记录（程序 ${mech.length} 条，模型 ${issues.length} 条${rev.repaired ? "，硬伤已修一轮" : ""}）</summary>
    ${note ? `<h3>样板覆盖提示</h3><p>${esc(note)}</p>` : ""}
    ${missingBlock}
    ${mechRows ? `<h3>程序核对</h3><ul class="plain-list">${mechRows}</ul>` : ""}
    ${issueRows ? `<h3>模型审稿</h3><ul class="plain-list">${issueRows}</ul>` : ""}
  </details>`;
}

function logHTML(job) {
  const log = (job.log || []).slice(-20);
  if (!log.length) return "";
  const rows = log.map((l) => `<li><span class="muted">${esc((l.ts || "").replace("T", " ").slice(0, 16))}</span> ${esc(l.msg)}</li>`).join("");
  return `<details class="report-card"><summary>过程日志（最近 ${log.length} 条）</summary>
    <ol class="job-log">${rows}</ol></details>`;
}

/* ---------- 操作 ---------- */

function postJSON(url, body) {
  return fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  }).then((r) => r.json());
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn || !currentJobId) return;
  const act = btn.dataset.act;
  const jid = encodeURIComponent(currentJobId);

  if (act === "outline-confirm") {
    if (outlineLoading) return;
    const text = (document.getElementById("outline-text") || {}).value || "";
    if (!text.trim()) { App.toast("大纲不能为空", "warn"); return; }
    outlineLoading = true;
    btn.disabled = true;
    btn.textContent = "正在重新列缺口…";
    postJSON(`/api/report/${jid}/outline`, { outline_md: text })
      .then((d) => {
        outlineLoading = false;
        if (d.error) { App.toast(d.error, "bad"); btn.disabled = false; btn.textContent = "确认大纲"; return; }
        App.toast("大纲已确认", "ok");
        loadJobs(currentJobId);
        loadJob(currentJobId);
      })
      .catch((err) => {
        outlineLoading = false;
        btn.disabled = false;
        btn.textContent = "确认大纲";
        App.toast("确认失败：" + err, "bad");
      });
    return;
  }

  if (act === "gaps-submit" || act === "gaps-skip") {
    const ids = act === "gaps-skip" ? [] : Array.from(document.querySelectorAll(".gap-line input:checked")).map((i) => i.value);
    btn.disabled = true;
    postJSON(`/api/report/${jid}/gaps`, { gap_ids: ids })
      .then((d) => {
        if (d.error) { App.toast(d.error, "bad"); btn.disabled = false; return; }
        App.toast(d.started === "fetch" ? "已开始补缺口，补完自动成文" : "已开始成文", "info");
        loadJobs(currentJobId);
        loadJob(currentJobId);
      })
      .catch((err) => { btn.disabled = false; App.toast("提交失败：" + err, "bad"); });
    return;
  }

  if (act === "delete-job") {
    const job = currentJob || {};
    if (REPORT_RUNNING.has(job.status)) { App.toast("任务还在进行中，等它结束或出错后再删", "warn"); return; }
    const files = [job.outline_path, job.report_path, job.docx_path]
      .filter(Boolean).map(baseName);
    const charts = (job.charts || []).length;
    const what = files.length ? `\n将删除：${files.join("、")}${charts ? `，以及 ${charts} 张图表` : ""}` : "";
    if (!window.confirm(`删除研报《${job.title}》？${what}\n任务记录与沉淀候选一并删除，不可恢复。`)) return;
    btn.disabled = true;
    btn.textContent = "正在删除…";
    postJSON(`/api/report/${jid}/delete`, {})
      .then((d) => {
        if (d.error) { App.toast(d.error, "bad"); btn.disabled = false; btn.textContent = "删除这份研报"; return; }
        App.toast(`已删除《${d.title || job.title}》${d.removed && d.removed.length ? `，清理 ${d.removed.length} 个文件` : ""}`, "ok");
        currentJobId = null;
        currentJob = null;
        history.replaceState(null, "", "/report");
        loadJobs();
      })
      .catch((err) => { btn.disabled = false; btn.textContent = "删除这份研报"; App.toast("删除失败：" + err, "bad"); });
    return;
  }

  if (act === "continue") {
    btn.disabled = true;
    postJSON(`/api/report/${jid}/continue`)
      .then((d) => {
        if (d.error) { App.toast(d.error, "bad"); btn.disabled = false; return; }
        App.toast("已继续执行", "info");
        loadJob(currentJobId);
        loadJobs(currentJobId);
      })
      .catch((err) => { btn.disabled = false; App.toast("继续失败：" + err, "bad"); });
    return;
  }

  if (act === "fallback") {
    if (!window.confirm("改走内置四段式底稿？行业管线停在这里，不再自动往下走。")) return;
    btn.disabled = true;
    postJSON(`/api/report/${jid}/fallback`)
      .then((d) => {
        if (d.error) { App.toast(d.error, "bad"); btn.disabled = false; return; }
        App.toast("已改用内置底稿，进度见首页", "info");
        loadJob(currentJobId);
        loadJobs(currentJobId);
      })
      .catch((err) => { btn.disabled = false; App.toast("切换失败：" + err, "bad"); });
    return;
  }

  if (act === "cand-apply") {
    const ids = Array.from(document.querySelectorAll(".cand-line input:checked")).map((i) => i.value);
    if (!ids.length) { App.toast("没有勾选任何候选", "warn"); return; }
    btn.disabled = true;
    btn.textContent = "正在写入 wiki…";
    postJSON(`/api/report/${jid}/candidates/apply`, { candidate_ids: ids })
      .then((d) => {
        if (d.error) { App.toast(d.error, "bad"); btn.disabled = false; btn.textContent = "写入选中的沉淀"; return; }
        const errs = d.errors || [];
        if (errs.length) App.toast(`写入 ${d.ok || 0} 条，失败 ${errs.length} 条：${errs.join("；")}`, "warn");
        else App.toast(`已写入 ${d.ok || 0} 条新知识，详情见日志页`, "ok");
        loadJob(currentJobId);
        loadJobs(currentJobId);
      })
      .catch((err) => { btn.disabled = false; btn.textContent = "写入选中的沉淀"; App.toast("写入失败：" + err, "bad"); });
  }
});

document.getElementById("job-select").addEventListener("change", (e) => {
  if (e.target.value) selectJob(e.target.value);
});

document.getElementById("job-refresh").addEventListener("click", () => {
  loadJobs(currentJobId);
  if (currentJobId) loadJob(currentJobId);
});

/* 进行中的任务卡（报告 / 补缺口） */
App.onTasks.push((tasks) => {
  const area = document.getElementById("job-tasks");
  if (!area) return;
  const mine = tasks.filter((t) => t.kind === "report" || t.kind === "fetch").slice(-6);
  area.innerHTML = mine.map((t) => App.taskCardHTML(t)).join("");
});

/* 阶段推进时自动刷新 */
setInterval(() => {
  if (document.hidden || !currentJobId) return;
  if (currentJob && REPORT_RUNNING.has(currentJob.status)) loadJob(currentJobId);
}, 2500);

document.addEventListener("DOMContentLoaded", () => {
  loadJobs(hashJobId());
  loadSourceLabels();
});
