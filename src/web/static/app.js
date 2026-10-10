/* 研报百科 前端交互（原生 JS，无框架） */

/* 1. 角标点击 → 右栏 iframe 打开 PDF 原页（仅页面视图有 iframe；问答/报告页走新标签打开） */
document.addEventListener("click", (e) => {
  const a = e.target.closest("sup.cite a");
  if (!a) return;
  const frame = document.getElementById("pdf-frame");
  if (!frame) return; // 无对照栏，保持默认新标签打开
  e.preventDefault();
  frame.hidden = false;
  frame.src = a.getAttribute("href");
  const ph = document.getElementById("pdf-placeholder");
  if (ph) ph.hidden = true;
  const close = document.getElementById("pdf-close");
  if (close) close.hidden = false;
});
document.addEventListener("click", (e) => {
  if (e.target.id !== "pdf-close") return;
  const frame = document.getElementById("pdf-frame");
  if (frame) { frame.hidden = true; frame.src = "about:blank"; }
  e.target.hidden = true;
  const ph = document.getElementById("pdf-placeholder");
  if (ph) ph.hidden = false;
});

/* 2. 首页过滤 */
const filter = document.getElementById("filter");
if (filter) {
  filter.addEventListener("input", () => {
    const q = filter.value.trim().toLowerCase();
    let shown = 0;
    document.querySelectorAll("tr.row").forEach((tr) => {
      const hit = !q || tr.dataset.search.toLowerCase().includes(q);
      tr.style.display = hit ? "" : "none";
      if (hit) shown += 1;
    });
    const count = document.getElementById("filter-count");
    if (count) count.textContent = q ? `匹配 ${shown} 页` : "";
  });
}

/* 3. 问答提交提示 */
const askForm = document.getElementById("ask-form");
if (askForm) {
  askForm.addEventListener("submit", () => {
    const hint = document.getElementById("ask-hint");
    if (hint) hint.textContent = "提问中：选页 + 作答约需十几秒，请勿关闭页面…";
  });
}

/* 4. 收件台上传 */
const dropzone = document.getElementById("dropzone");
if (dropzone) {
  const input = document.getElementById("file-input");
  const progress = document.getElementById("upload-progress");
  const results = document.getElementById("upload-results");

  const send = (fileList) => {
    const files = Array.from(fileList).filter((f) => f.name.toLowerCase().endsWith(".pdf"));
    if (!files.length) { progress.hidden = false; progress.textContent = "只接受 PDF 文件。"; return; }
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    progress.hidden = false;
    progress.textContent = `已接收 ${files.length} 份，模型归类中，每份约十几秒…`;
    results.innerHTML = "";
    fetch("/upload/do", { method: "POST", body: fd })
      .then((r) => r.json())
      .then((d) => {
        progress.hidden = true;
        const rows = d.results.map((r) => `
          <tr>
            <td>${r.file || ""}</td>
            <td>${r.ok ? "✓ 归类成功" : "✗ 失败"}</td>
            <td>${r.ok ? `${r.renamed}<br><span class="muted">${r.target}</span>` : `<span class="muted">${r.error || ""}</span>`}</td>
            <td class="muted">${r.reason || ""}</td>
          </tr>`).join("");
        results.innerHTML = `<table><thead><tr><th>原文件</th><th>结果</th><th>新名字 / 目录</th><th>依据</th></tr></thead><tbody>${rows}</tbody></table>`;
      })
      .catch((err) => { progress.hidden = false; progress.textContent = "上传失败：" + err; });
  };

  dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("drag"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag"));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("drag");
    if (e.dataTransfer && e.dataTransfer.files.length) send(e.dataTransfer.files);
  });
  if (input) input.addEventListener("change", () => { if (input.files.length) send(input.files); });
}

/* 5. 摄入执行 */
const ingestForm = document.getElementById("ingest-form");
if (ingestForm) {
  ingestForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const paths = Array.from(ingestForm.querySelectorAll('input[name=path]:checked')).map((c) => c.value);
    const hint = document.getElementById("ingest-hint");
    const btn = document.getElementById("ingest-btn");
    const log = document.getElementById("ingest-log");
    if (!paths.length) { hint.textContent = "没有勾选任何新文件。"; return; }
    btn.disabled = true;
    hint.textContent = `摄入中：${paths.length} 份，每份 20 页约 1–2 分钟，请保持页面打开…`;
    log.hidden = false;
    log.textContent = "";
    fetch("/ingest/do", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths }),
    })
      .then((r) => r.json())
      .then((d) => {
        btn.disabled = false;
        hint.textContent = "";
        log.textContent = d.log + (d.problems && d.problems.length
          ? "\n[校验问题]\n" + d.problems.join("\n")
          : "\n[全库校验通过]");
        log.textContent += "\n\n3 秒后刷新列表…";
        setTimeout(() => location.reload(), 3000);
      })
      .catch((err) => { btn.disabled = false; hint.textContent = "摄入请求失败：" + err; });
  });
}

/* 6. 审校执行 */
const auditBtn = document.getElementById("audit-btn");
if (auditBtn) {
  auditBtn.addEventListener("click", () => {
    const hint = document.getElementById("audit-hint");
    auditBtn.disabled = true;
    hint.textContent = "审校中：逐页矛盾检测与缺口分析，约需几十秒…";
    fetch("/audit/run", { method: "POST" })
      .then((r) => r.json())
      .then((d) => {
        const c = d.counts || {};
        hint.textContent = `完成：矛盾 ${c["矛盾"] || 0}、过期 ${c["过期"] || 0}、未摄入 ${c["未摄入文件"] || 0} 文件、孤立 ${c["孤立"] || 0}，正在打开报告…`;
        location.href = "/outputs/" + encodeURIComponent(d.file);
      })
      .catch((err) => { auditBtn.disabled = false; hint.textContent = "审校失败：" + err; });
  });
}
