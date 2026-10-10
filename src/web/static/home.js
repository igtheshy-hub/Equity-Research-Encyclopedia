/* 首页：提问框跳问答页；文件拖入/点击上传；取名入库与摄入进度 */

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("home-ask");
  const input = document.getElementById("home-q");
  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const q = (input.value || "").trim();
      if (q) location.href = "/ask?q=" + encodeURIComponent(q);
    });
  }

  const dz = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const hint = document.getElementById("upload-hint");
  if (!dz || !fileInput) return;

  const send = (fileList) => {
    const files = Array.from(fileList);
    const ok = files.filter((f) => /\.(pdf|docx)$/i.test(f.name));
    const bad = files.filter((f) => !/\.(pdf|docx)$/i.test(f.name));
    if (bad.length) {
      const hasDoc = bad.some((f) => /\.doc$/i.test(f.name));
      hint.textContent = hasDoc
        ? "不支持的文件：" + bad.map((f) => f.name).join("、") + "（旧版 .doc 请先在 Word 里另存为 .docx）"
        : "只接受 PDF 与 Word(.docx)：" + bad.map((f) => f.name).join("、");
    } else {
      hint.textContent = "";
    }
    if (!ok.length) return;
    const fd = new FormData();
    ok.forEach((f) => fd.append("files", f));
    hint.textContent = `已接收 ${ok.length} 份，正在开始处理…`;
    fetch("/api/upload", { method: "POST", body: fd })
      .then((r) => r.json().then((d) => ({ status: r.status, body: d })))
      .then(({ status, body: d }) => {
        if (status !== 200) {
          hint.textContent = d.error || "上传失败";
          return;
        }
        hint.textContent = `已接收 ${d.saved.length} 份` + (d.rejected && d.rejected.length ? `，拒绝 ${d.rejected.length} 份（只接受 PDF/.docx）` : "") + "，处理进度见下方";
      })
      .catch((err) => { hint.textContent = "上传失败：" + err; });
  };

  dz.addEventListener("click", () => fileInput.click());
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("drag");
    if (e.dataTransfer && e.dataTransfer.files.length) send(e.dataTransfer.files);
  });
  fileInput.addEventListener("change", () => { if (fileInput.files.length) send(fileInput.files); });
});

/* 两条进度（取名入库 / 摄入）来自全局任务轮询 */
App.onTasks.push((tasks) => {
  const area = document.getElementById("task-area");
  if (!area) return;
  const mine = tasks.filter((t) => t.kind === "classify" || t.kind === "ingest");
  area.innerHTML = mine.map((t) => App.taskCardHTML(t)).join("");
});
