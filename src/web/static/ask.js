/* 问答页：聊天气泡流、独立轮询、进一步查询、提取、双栏分屏 */

const PANE_HTML = `
  <div class="pane-head">
    <select class="conv-select" title="切换会话"></select>
    <button class="ghost sm new-conv" title="开一个空白会话">新会话</button>
    <button class="ghost sm extract" title="把本会话答案里值得进知识库的内容写进 wiki">提取</button>
  </div>
  <div class="pane-msgs"><p class="muted pane-empty">输入问题开始提问，答案后台生成，切页不丢。</p></div>
  <div class="pane-input">
    <textarea rows="2" placeholder="请输入文本"></textarea>
    <button class="primary send">发送</button>
  </div>`;

function makePane(el) {
  const pane = { el, convId: null, convs: [], raw: "", timer: null };
  el.innerHTML = PANE_HTML;
  pane.$msgs = el.querySelector(".pane-msgs");
  pane.$select = el.querySelector(".conv-select");
  pane.$text = el.querySelector("textarea");
  pane.$send = el.querySelector(".send");

  pane.loadConvs = function (keepCurrent) {
    return fetch("/api/conversations").then((r) => r.json()).then((d) => {
      pane.convs = d.items || [];
      pane.$select.innerHTML = pane.convs.map((c) =>
        `<option value="${c.id}">${App.escapeHtml(c.title || "未命名")}（${c.messages}）</option>`).join("");
      if (keepCurrent && pane.convId && pane.convs.some((c) => c.id === pane.convId)) {
        pane.$select.value = pane.convId;
      } else {
        pane.selectConv((pane.convs[0] || {}).id || null);
      }
    }).catch(() => {});
  };

  pane.selectConv = function (cid) {
    pane.convId = cid;
    if (cid) pane.$select.value = cid;
    pane.raw = "";
    pane.$msgs.innerHTML = cid ? '<p class="muted">加载中…</p>' : '<p class="muted pane-empty">点击「新会话」开始。</p>';
    pane.refresh(true);
  };

  pane.refresh = function (jumpBottom) {
    if (!pane.convId) return;
    fetch("/api/conversations/" + pane.convId)
      .then((r) => r.json())
      .then((conv) => {
        if (conv.error) return;
        const raw = JSON.stringify(conv.messages);
        const title = conv.title || "未命名";
        const opt = pane.$select.querySelector(`option[value="${conv.id}"]`);
        if (opt && opt.textContent.indexOf(title) !== 0) {
          opt.textContent = `${title}（${conv.messages.length}）`;
        }
        if (raw === pane.raw) return;
        pane.raw = raw;
        pane.render(conv.messages);
        if (jumpBottom || paneNearBottom()) paneScrollBottom();
      })
      .catch(() => {});
  };

  pane.render = function (msgs) {
    if (!msgs.length) {
      pane.$msgs.innerHTML = '<p class="muted pane-empty">输入问题开始提问。</p>';
      return;
    }
    pane.$msgs.innerHTML = msgs.map((m) => renderMsg(m)).join("");
    App.enrichCiteLinks(pane.$msgs);
  };

  pane.send = function () {
    const q = (pane.$text.value || "").trim();
    if (!q) return;
    let convId = pane.convId;
    const go = () => {
      pane.$text.value = "";
      pane.$send.disabled = true;
      fetch("/api/ask", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, conv_id: convId }),
      })
        .then((r) => r.json())
        .then((d) => {
          pane.$send.disabled = false;
          if (d.error) { App.toast(d.error, "bad"); return; }
          pane.convId = d.conv_id;
          pane.raw = "";
          pane.refresh(true);
          setTimeout(() => pane.loadConvs(true), 800);
        })
        .catch((err) => { pane.$send.disabled = false; App.toast("发送失败：" + err, "bad"); });
    };
    if (convId) { go(); return; }
    fetch("/api/conversations", { method: "POST" })
      .then((r) => r.json())
      .then((d) => { convId = d.conv.id; go(); });
  };

  el.querySelector(".new-conv").addEventListener("click", () => {
    fetch("/api/conversations", { method: "POST" })
      .then((r) => r.json())
      .then((d) => pane.loadConvs().then(() => pane.selectConv(d.conv.id)));
  });

  el.querySelector(".extract").addEventListener("click", () => {
    if (!pane.convId) { App.toast("当前没有会话", "warn"); return; }
    fetch("/api/distill/propose", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conv_id: pane.convId }),
    })
      .then((r) => r.json())
      .then((d) => {
        if (!(d.items || []).length) { App.toast("本会话没有待提取的新知识", "info"); return; }
        App.openExtractModal(d.items, "当前会话的可沉淀新知识，勾选要写入 wiki 的行");
      })
      .catch((err) => App.toast("查询提案失败：" + err, "bad"));
  });

  pane.$select.addEventListener("change", () => pane.selectConv(pane.$select.value));
  pane.$send.addEventListener("click", pane.send);
  pane.$text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); pane.send(); }
  });

  pane.timer = setInterval(() => { if (!document.hidden) pane.refresh(false); }, 1800);
  return pane;

  function paneNearBottom() {
    return pane.$msgs.scrollHeight - pane.$msgs.scrollTop - pane.$msgs.clientHeight < 80;
  }
  function paneScrollBottom() {
    pane.$msgs.scrollTop = pane.$msgs.scrollHeight;
  }
}

function renderMsg(m) {
  if (m.role === "user") {
    return `<div class="msg user"><div class="bubble">${App.escapeHtml(m.content)}</div></div>`;
  }
  // assistant
  if (m.status === "running") {
    return `<div class="msg bot"><div class="bubble thinking"><span class="dots"><i></i><i></i><i></i></span> 正在查阅知识库并作答…</div></div>`;
  }
  if (m.kind === "error" || m.status === "error") {
    return `<div class="msg bot"><div class="bubble bubble-err">${App.escapeHtml(m.content || "出错了")}</div></div>`;
  }
  if (m.kind === "report_ack") {
    return `<div class="msg bot"><div class="bubble">
      ${App.escapeHtml(m.content || "")}
      <p><a href="/outputs" target="_blank" rel="noopener">前往产出页查看进度 →</a></p>
    </div></div>`;
  }
  if (m.kind === "referral") {
    return renderReferral(m);
  }
  return renderAnswer(m);
}

function renderAnswer(m) {
  const bits = [];
  if (m.deep) bits.push(`<span class="tag deep">原文深查</span>`);
  if (m.selected && m.selected.length) {
    bits.push(`<span class="meta-bit">参考页面：${App.escapeHtml(m.selected.join("、"))}${m.reason ? "（" + App.escapeHtml(m.reason) + "）" : ""}</span>`);
  }
  let warn = "";
  if (m.validation && !m.validation.ok) {
    warn = `<div class="warn-strip">答案锚点校验未通过：${App.escapeHtml((m.validation.violations || []).join("；"))}，请谨慎使用。</div>`;
  }
  let prop = "";
  if (m.proposal && m.proposal.lines && m.proposal.lines.length) {
    prop = `<div class="prop-note">有 ${m.proposal.lines.length} 条可沉淀新知识，点上方「提取」处理</div>`;
  }
  return `<div class="msg bot"><div class="bubble">
    ${bits.length ? `<div class="meta-row">${bits.join("")}</div>` : ""}
    ${warn}<div class="md">${m.html || App.escapeHtml(m.content || "")}</div>${prop}
  </div></div>`;
}

function renderReferral(m) {
  const cands = m.candidates || [];
  let inner = `<p>${App.escapeHtml(m.content || "")}</p>`;
  if (cands.length) {
    inner += `<div class="cand-list">` + cands.map((c, i) => `
      <label class="cand"><input type="checkbox" checked value="${App.escapeHtml(c.file)}">
        <span>${App.escapeHtml(c.file)}</span>
        <span class="muted">${App.escapeHtml(c.reason || "")}</span></label>`).join("") + `</div>`;
  }
  const btnText = cands.length ? "进一步查询选中的文件" : "进一步查询所有文件";
  inner += `<button class="ghost deep-btn" data-mid="${m.id}">${btnText}</button>`;
  return `<div class="msg bot"><div class="bubble bubble-ref">${inner}</div></div>`;
}

/* 进一步查询（deep search）按钮 */
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".deep-btn");
  if (!btn) return;
  const msgsEl = btn.closest(".pane-msgs");
  const paneEl = btn.closest(".pane");
  const mid = btn.dataset.mid;
  const convId = panes.find((p) => p.el === paneEl).convId;
  const checked = msgsEl ? Array.from(msgsEl.querySelectorAll(".cand input:checked")).map((c) => c.value) : [];
  const body = { conv_id: convId, msg_id: mid };
  if (checked.length) body.files = checked;
  btn.disabled = true;
  btn.textContent = "正在深查 raw 原文，约半分钟…";
  fetch("/api/deep", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
    .then((r) => r.json())
    .then((d) => {
      if (d.error) { btn.disabled = false; btn.textContent = "进一步查询（重试）"; App.toast(d.error, "bad"); return; }
      App.toast("已开始深查，结果稍后出现在对话里", "info");
    })
    .catch((err) => { btn.disabled = false; btn.textContent = "进一步查询（重试）"; App.toast("深查请求失败：" + err, "bad"); });
});

/* ---------- 分栏 ---------- */

const panes = [];
document.addEventListener("DOMContentLoaded", () => {
  const wrap = document.getElementById("panes");
  const divider = document.getElementById("vdivider");
  const toggle = document.getElementById("split-toggle");
  const paneEls = wrap.querySelectorAll(".pane");
  panes.push(makePane(paneEls[0]));
  panes.push(makePane(paneEls[1]));

  const applyRatio = (pct) => {
    pct = Math.max(20, Math.min(80, pct));
    paneEls[0].style.flex = `0 0 ${pct}%`;
    paneEls[1].style.flex = "1 1 auto";
    divider.style.flex = "0 0 6px";
  };

  const setSplit = (on, restore) => {
    paneEls[1].hidden = !on;
    divider.hidden = !on;
    toggle.textContent = on ? "关闭分栏" : "添加会话窗口";
    document.body.classList.toggle("split-ask", on);
    if (on) {
      localStorage.setItem("askSplit", "1");
      if (restore) {
        const saved = parseFloat(localStorage.getItem("askSplitRatio") || "50");
        applyRatio(saved);
      }
      const p0 = panes[0], p1 = panes[1];
      if (!p1.convId) {
        p1.loadConvs().then(() => {
          if (p1.convId === p0.convId && p1.convs.length > 1) {
            const other = p1.convs.find((c) => c.id !== p0.convId);
            if (other) p1.selectConv(other.id);
          } else if (!p1.convId) {
            fetch("/api/conversations", { method: "POST" })
              .then((r) => r.json())
              .then((d) => p1.loadConvs().then(() => p1.selectConv(d.conv.id)));
          }
        });
      }
    } else {
      localStorage.removeItem("askSplit");
    }
  };

  toggle.addEventListener("click", () => setSplit(paneEls[1].hidden, true));

  let dragging = false;
  divider.addEventListener("mousedown", (e) => { dragging = true; e.preventDefault(); });
  document.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const rect = wrap.getBoundingClientRect();
    const pct = ((e.clientX - rect.left) / rect.width) * 100;
    applyRatio(pct);
    localStorage.setItem("askSplitRatio", String(pct));
  });
  document.addEventListener("mouseup", () => { dragging = false; });

  /* 初始：默认选最近会话；恢复分栏状态 */
  panes[0].loadConvs();
  if (localStorage.getItem("askSplit") === "1") setSplit(true, true);

  /* 首页跳转带 ?q= 时：新会话自动开问 */
  const q = new URLSearchParams(location.search).get("q");
  if (q) {
    history.replaceState(null, "", "/ask");
    const p0 = panes[0];
    fetch("/api/conversations", { method: "POST" })
      .then((r) => r.json())
      .then((d) => {
        p0.convId = d.conv.id;
        p0.$text.value = q;
        p0.send();
        p0.loadConvs(true);
      });
  }
});

App.onDistillApplied = () => panes.forEach((p) => { p.raw = ""; p.refresh(false); });
