/* 原文对照查看器：PDF.js 渲染（定位页 + 文本层高亮 + 缩放）与 docx HTML（定位段落） */
import * as pdfjsLib from "/static/vendor/pdfjs/pdf.min.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdfjs/pdf.worker.min.mjs";

const body = document.body;
const file = body.dataset.file || "";
const loc = body.dataset.loc || "";
const hl = (body.dataset.hl || "").replace(/\s+/g, "");

const $scroll = document.getElementById("v-scroll");
const $name = document.getElementById("v-name");
const $loc = document.getElementById("v-loc");
const $prog = document.getElementById("v-progress");

function setLocLabel() {
  if (!loc) { $loc.textContent = ""; return; }
  $loc.textContent = loc.startsWith("para")
    ? `定位到第 ${parseInt(loc.slice(4), 10)} 段`
    : `定位到第 ${parseInt(loc.slice(1), 10)} 页`;
}

function fail(msg) {
  $scroll.innerHTML = `<div class="viewer-fail">
    <p>${msg}</p>
    <p><a href="/file/${encodeURIComponent(file)}" target="_blank" rel="noopener">直接打开原文件</a></p></div>`;
}

const isDocx = /\.docx$/i.test(file);
setLocLabel();

/* ---------- Word 模式 ---------- */

if (isDocx) {
  document.getElementById("v-zoom-out").addEventListener("click", () => zoomDocx(-1));
  document.getElementById("v-zoom-in").addEventListener("click", () => zoomDocx(1));
  document.getElementById("v-zoom-fit").addEventListener("click", () => {
    $scroll.style.fontSize = "";
  });
  let fs = 15;
  function zoomDocx(dir) {
    fs = Math.max(11, Math.min(24, fs + dir * 1.5));
    $scroll.style.fontSize = fs + "px";
  }
  fetch("/file/" + encodeURIComponent(file))
    .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.text(); })
    .then((html) => {
      $scroll.innerHTML = `<div class="docx-view">${html}</div>`;
      const blocks = Array.from($scroll.querySelectorAll("[data-n]"));
      if (loc.startsWith("para")) {
        const n = parseInt(loc.slice(4), 10);
        const el = $scroll.querySelector(`[data-n="${n}"]`);
        if (el) {
          el.classList.add("hl-block");
          el.scrollIntoView({ block: "center" });
        } else {
          $loc.textContent += "（未找到该段落号）";
        }
      }
      if (hl) {
        let hit = 0;
        blocks.forEach((el) => {
          if (el.textContent.replace(/\s+/g, "").includes(hl)) {
            el.classList.add("hl-soft");
            hit += 1;
          }
        });
        if (hit && !loc) blocks.find((el) => el.classList.contains("hl-soft")).scrollIntoView({ block: "center" });
      }
      $prog.textContent = `共 ${blocks.length} 块`;
    })
    .catch((e) => fail("Word 文档打开失败：" + e.message));
} else {
  /* ---------- PDF 模式 ---------- */
  let pdf = null;
  let mode = "fit";
  let manualScale = 0;
  const rendered = {};

  function curScale() {
    if (mode === "manual") return manualScale;
    return null; // 逐页适宽
  }

  async function renderPage(n) {
    if (rendered[n] && rendered[n].scaleKey === scaleKey(n)) return;
    const page = await pdf.getPage(n);
    const base = page.getViewport({ scale: 1 });
    const avail = Math.max(280, $scroll.clientWidth - 28);
    const scale = mode === "manual" ? manualScale : avail / base.width;
    const viewport = page.getViewport({ scale });
    let holder = $scroll.querySelector(`.pdf-page[data-page="${n}"]`);
    if (!holder) {
      holder = document.createElement("div");
      holder.className = "pdf-page";
      holder.dataset.page = String(n);
      $scroll.appendChild(holder);
    }
    holder.innerHTML = "";
    holder.style.width = Math.floor(viewport.width) + "px";
    const label = document.createElement("div");
    label.className = "pdf-pageno";
    label.textContent = `第 ${n} / ${pdf.numPages} 页`;
    holder.appendChild(label);
    const canvas = document.createElement("canvas");
    canvas.width = viewport.width * (window.devicePixelRatio || 1);
    canvas.height = viewport.height * (window.devicePixelRatio || 1);
    canvas.style.width = "100%";
    const ctx = canvas.getContext("2d");
    ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);
    await page.render({ canvasContext: ctx, viewport }).promise;
    holder.appendChild(canvas);
    const tl = document.createElement("div");
    tl.className = "textLayer";
    holder.appendChild(tl);
    const textContent = await page.getTextContent();
    const tlObj = new pdfjsLib.TextLayer({ textContentSource: textContent, container: tl, viewport });
    await tlObj.render();
    rendered[n] = { scaleKey: scaleKey(n) };
    if (isTargetPage(n)) {
      holder.scrollIntoView({ block: "start" });
      if (hl) {
        const okHit = highlight(tl, hl);
        if (!okHit) highlightLoose(tl, hl.slice(0, 12));
      }
      delete holder.dataset.pending;
    }
    $prog.textContent = `已渲染 ${Object.keys(rendered).length}/${pdf.numPages} 页`;
  }

  function scaleKey(n) { return mode + ":" + (mode === "manual" ? manualScale.toFixed(2) : "w" + $scroll.clientWidth); }
  function isTargetPage(n) { return loc.startsWith("p") && parseInt(loc.slice(1), 10) === n; }

  function highlight(tl, text) {
    const spans = Array.from(tl.children);
    const norm = (s) => s.replace(/\s+/g, "");
    let acc = "";
    const owner = [];
    spans.forEach((sp, si) => {
      const t = norm(sp.textContent);
      for (let i = 0; i < t.length; i++) owner.push(si);
      acc += t;
    });
    const at = acc.indexOf(text);
    if (at < 0) return false;
    const end = at + text.length;
    for (let i = at; i < end && i < owner.length; i++) spans[owner[i]].classList.add("hl");
    return true;
  }

  function highlightLoose(tl, text) { highlight(tl, text); }

  async function renderAll() {
    Object.keys(rendered).forEach((k) => delete rendered[k]);
    const target = loc.startsWith("p") ? parseInt(loc.slice(1), 10) : 1;
    const order = [];
    if (target >= 1 && target <= pdf.numPages) order.push(target);
    for (let n = 1; n <= pdf.numPages; n++) if (n !== target) order.push(n);
    for (const n of order) {
      if ($scroll.dataset.zoomGen !== String(zoomGen)) return; // 缩放被重触发，本轮作废
      await renderPage(n);
    }
    if ($scroll.dataset.zoomGen === String(zoomGen)) $prog.textContent = `共 ${pdf.numPages} 页`;
  }

  let zoomGen = 0;
  async function rerender() {
    zoomGen += 1;
    $scroll.dataset.zoomGen = String(zoomGen);
    await renderAll();
  }

  document.getElementById("v-zoom-in").addEventListener("click", () => {
    const s = mode === "manual" ? manualScale : 1.25;
    manualScale = s * 1.25;
    mode = "manual";
    rerender();
  });
  document.getElementById("v-zoom-out").addEventListener("click", () => {
    const s = mode === "manual" ? manualScale : 1.0;
    manualScale = Math.max(0.3, s * 0.8);
    mode = "manual";
    rerender();
  });
  document.getElementById("v-zoom-fit").addEventListener("click", () => {
    mode = "fit";
    rerender();
  });
  window.addEventListener("resize", () => { if (mode === "fit") rerender(); });

  pdfjsLib.getDocument({ url: "/file/" + encodeURIComponent(file) })
    .promise
    .then(async (doc) => {
      pdf = doc;
      $scroll.innerHTML = "";
      $scroll.dataset.zoomGen = "0";
      zoomGen = 0;
      await rerender();
    })
    .catch((e) => fail("PDF 打开失败：" + (e && e.message ? e.message : e)));
}
