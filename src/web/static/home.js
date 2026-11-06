/* 首页：提问框跳问答页；文件拖入/点击上传；取名入库与摄入进度 */

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("home-ask");
  const input = document.getElementById("home-q");
  const modelSel = document.getElementById("model-select");
  if (modelSel) {
    // 选择记在本地，下次打开还是它；问答页也读同一个键
    const saved = localStorage.getItem("askModel");
    if (saved && Array.from(modelSel.options).some((o) => o.value === saved)) modelSel.value = saved;
    modelSel.addEventListener("change", () => localStorage.setItem("askModel", modelSel.value));
  }
  // 添加模型：白名单服务商 + 双协议探活，两个端点都通了才保存并立即出现在下拉里
  const addBtn = document.getElementById("model-add-btn");
  const addModal = document.getElementById("model-modal");
  if (addBtn && addModal) {
    const result = document.getElementById("model-add-result");
    const submit = document.getElementById("model-add-submit");
    const showResult = (html, cls) => {
      result.hidden = false;
      result.className = "model-add-result " + (cls || "");
      result.innerHTML = html;
    };
    addBtn.addEventListener("click", () => {
      result.hidden = true;
      result.innerHTML = "";
      addModal.hidden = false;
      document.getElementById("model-id").focus();
    });
    addModal.addEventListener("click", (e) => {
      if (e.target === addModal) addModal.hidden = true;
    });
    submit.addEventListener("click", () => {
      const provider = document.getElementById("model-provider").value;
      const modelId = (document.getElementById("model-id").value || "").trim();
      const label = (document.getElementById("model-label").value || "").trim();
      const apiKey = (document.getElementById("model-key").value || "").trim();
      const anth = (document.getElementById("model-anthropic").value || "").trim();
      if (!modelId) { showResult("请填写模型 ID", "bad"); return; }
      submit.disabled = true;
      submit.textContent = "正在测试两个端点…";
      showResult("正在测试主流程与救火端点，最多约一分钟…", "");
      fetch("/api/models", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({provider, model_id: modelId, label,
                              api_key: apiKey, anthropic_model: anth}),
      })
        .then((r) => r.json().then((d) => ({status: r.status, body: d})))
        .then(({status, body}) => {
          if (status !== 200) {
            const lines = [body.error || "添加失败"];
            if (body.chat) lines.push(body.chat);
            if (body.rescue) lines.push(body.rescue);
            showResult(lines.map((x) => `<div>${App.escapeHtml(x)}</div>`).join(""), "bad");
            return;
          }
          const m = body.model;
          if (modelSel) {
            const opt = document.createElement("option");
            opt.value = m.id;
            opt.textContent = m.label || m.id;
            modelSel.appendChild(opt);
            modelSel.value = m.id;
          }
          window.MODEL_IDS = (window.MODEL_IDS || []).concat([m.id]);
          localStorage.setItem("askModel", m.id);
          showResult(`已添加 ${App.escapeHtml(m.label || m.id)}；${App.escapeHtml(body.chat)}；${App.escapeHtml(body.rescue)}`, "ok");
          addModal.hidden = true;
          App.toast("模型已添加并选中：" + (m.label || m.id), "ok");
        })
        .catch((err) => showResult("请求失败：" + App.escapeHtml(err), "bad"))
        .finally(() => {
          submit.disabled = false;
          submit.textContent = "测试并添加";
        });
    });
  }

  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const q = (input.value || "").trim();
      if (!q) return;
      const model = modelSel ? modelSel.value : "";
      location.href = "/ask?q=" + encodeURIComponent(q) + "&model=" + encodeURIComponent(model);
    });
  }

  const dz = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const hint = document.getElementById("upload-hint");
  const focusInput = document.getElementById("focus-input");
  const focusEnabled = document.getElementById("focus-enabled");
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
    // 首页下拉选中的模型对取名与摄入同样生效，不再需要改 config.json
    const uploadModel = modelSel ? modelSel.value : (localStorage.getItem("askModel") || "");
    if (uploadModel) fd.append("model", uploadModel);

    // 关注点：填了内容且勾了开关才带上
    const focusText = (focusInput && focusInput.value || "").trim();
    if (focusText && focusEnabled && focusEnabled.checked) {
      const lines = focusText.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
      if (lines.length) {
        fd.append("focus", JSON.stringify(lines));
        fd.append("use_focus", "1");
      }
    }

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
  renderProposalCard(area);
  // 摄入刚完成（或刚开始跑）时刷新一次提案数：新报告可能带来新的建页建议
  const hadIngest = tasks.some((t) => t.kind === "ingest");
  if (hadIngest && !prevHadIngest) refreshProposalCount();
  prevHadIngest = hadIngest;
});

/* 待建页提案：摄入完成卡片附「N 个建页建议」 */
let ppCount = null;
let prevHadIngest = false;

function refreshProposalCount() {
  fetch("/api/page-proposals/pending", { cache: "no-cache" })
    .then((r) => r.json())
    .then((d) => {
      ppCount = (d.items || []).length;
      renderProposalCard(document.getElementById("task-area"));
    })
    .catch(() => {});
}

function renderProposalCard(area) {
  if (!area) return;
  let card = document.getElementById("pp-card");
  if (!ppCount) { if (card) card.remove(); return; }
  if (!card) {
    card = document.createElement("div");
    card.id = "pp-card";
    card.className = "task-card done";
    area.appendChild(card);
  }
  card.innerHTML = `<div class="tc-top"><span class="tc-kind">建页建议</span>
    <span class="tc-label">本次摄入后共有 ${ppCount} 个待建页建议（完整性建议，建不建由你拍板）</span>
    <button class="sm primary" id="pp-open">查看建页建议</button></div>`;
  card.querySelector("#pp-open").addEventListener("click", () => {
    fetch("/api/page-proposals/pending", { cache: "no-cache" })
      .then((r) => r.json())
      .then((d) => {
        if ((d.items || []).length) App.openPageProposalModal(d.items);
        else App.toast("建页建议已处理完毕", "info");
      })
      .catch(() => {});
  });
}

App.onPageProposalsChanged = refreshProposalCount;
refreshProposalCount();
