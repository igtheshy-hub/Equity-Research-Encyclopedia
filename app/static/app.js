// 全站交互：拖拽上传 / 主题切换 / 拷贝反馈 / 引用悬浮
(function () {
    const root = document.documentElement;

    // —— 主题切换（持久化）——
    const saved = localStorage.getItem("wiki-theme");
    if (saved) root.setAttribute("data-theme", saved);
    else if (window.matchMedia("(prefers-color-scheme: dark)").matches)
        root.setAttribute("data-theme", "dark");
    document.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-theme-toggle]");
        if (!btn) return;
        const cur = root.getAttribute("data-theme") || "light";
        const next = cur === "dark" ? "light" : "dark";
        root.setAttribute("data-theme", next);
        localStorage.setItem("wiki-theme", next);
        btn.textContent = next === "dark" ? "☀" : "☾";
    });

    // —— 拷贝反馈 ——
    let flashTimer = null;
    function flash(msg) {
        let f = document.querySelector(".copy-flash");
        if (!f) {
            f = document.createElement("div");
            f.className = "copy-flash";
            document.body.appendChild(f);
        }
        f.textContent = msg;
        f.classList.add("show");
        clearTimeout(flashTimer);
        flashTimer = setTimeout(() => f.classList.remove("show"), 1500);
    }
    document.addEventListener("click", async (e) => {
        const btn = e.target.closest("[data-copy]");
        if (!btn) return;
        const text = btn.getAttribute("data-copy");
        try {
            await navigator.clipboard.writeText(text);
            flash("已复制 ✓");
        } catch (_) {
            const ta = document.createElement("textarea");
            ta.value = text; document.body.appendChild(ta);
            ta.select(); document.execCommand("copy"); document.body.removeChild(ta);
            flash("已复制 ✓");
        }
    });

    // —— 全局拖拽上传 ——
    // 在任意页面拖 PDF → 全屏 DropZone；松手 → 上传到 /api/ingest/upload + 进度卡
    const overlay = document.createElement("div");
    overlay.className = "dropzone-overlay";
    overlay.innerHTML = `
        <div class="pane">
            <div class="big-icon">⬆</div>
            <h2>松手上传 PDF</h2>
            <p>系统会自动抽取并提炼为知识页（带 LLM 时立即可用，否则标"等重试"）</p>
        </div>`;
    document.body.appendChild(overlay);

    let dragCount = 0;
    function isFileDrag(e) {
        return e.dataTransfer && e.dataTransfer.types
            && Array.from(e.dataTransfer.types).includes("Files");
    }
    window.addEventListener("dragenter", (e) => {
        if (!isFileDrag(e)) return;
        dragCount++;
        overlay.classList.add("show");
    });
    window.addEventListener("dragover", (e) => { e.preventDefault(); });
    window.addEventListener("dragleave", (e) => {
        dragCount = Math.max(0, dragCount - 1);
        if (dragCount === 0) overlay.classList.remove("show");
    });
    window.addEventListener("drop", async (e) => {
        e.preventDefault();
        dragCount = 0;
        overlay.classList.remove("show");
        const files = Array.from(e.dataTransfer.files || []).filter(f => /\.pdf$/i.test(f.name));
        if (!files.length) return;
        for (const f of files) await uploadOne(f);
    });

    async function uploadOne(file) {
        const ui = renderProgress(file.name);
        const fd = new FormData();
        fd.append("file", file);
        try {
            const xhr = new XMLHttpRequest();
            xhr.open("POST", "/api/ingest/upload");
            ui.setStep("active", 1);
            xhr.upload.onprogress = (ev) => {
                if (ev.lengthComputable) {
                    const pct = Math.min(100, (ev.loaded / ev.total) * 50);  // 上传占 0–50%
                    ui.setProgress(pct);
                }
            };
            xhr.onload = () => {
                if (xhr.status >= 200 && xhr.status < 300) {
                    let data; try { data = JSON.parse(xhr.responseText); } catch (_) { data = {}; }
                    ui.setStep("done", 1);
                    ui.setStep("active", 2);
                    ui.setProgress(70);
                    setTimeout(() => {
                        ui.setStep("done", 2);
                        ui.setStep("active", 3);
                        ui.setProgress(95);
                        setTimeout(() => {
                            ui.setStep("done", 3);
                            ui.setProgress(100);
                            let link = null;
                            if (data && Array.isArray(data.results)) {
                                const ok = data.results.find(r => r && r.ok);
                                if (ok && ok.report_id) link = `/audit`;
                            } else if (data && data.report_id) {
                                link = `/audit`;
                            }
                            const msg = (data && data.count > 1)
                                ? `批量完成 ${data.ok_count}/${data.count}`
                                : ((data && data.results && data.results[0])
                                    ? (data.results[0].status || "完成")
                                    : "完成");
                            ui.done(msg, link);
                            htmx.ajax("GET", "/api/ingest/library/pane",
                                {target: "#library-pane", swap: "innerHTML"});
                            setTimeout(() => { location.reload(); }, 800);
                        }, 300);
                    }, 250);
                } else {
                    ui.fail(`HTTP ${xhr.status}`);
                }
            };
            xhr.onerror = () => ui.fail("网络错误");
            xhr.send(fd);
        } catch (err) {
            ui.fail(err.message || "上传失败");
        }
    }

    function renderProgress(name) {
        const root = document.createElement("div");
        root.className = "upload-progress show";
        root.innerHTML = `
            <h4>摄取 ${escapeHtml(name)}</h4>
            <div class="bar"><div></div></div>
            <ul class="steps">
                <li data-step="1"><span class="dot"></span>上传</li>
                <li data-step="2"><span class="dot"></span>抽取 + 提炼</li>
                <li data-step="3"><span class="dot"></span>落库</li>
            </ul>
            <div class="done-line" style="margin-top:8px;font-size:12px;"></div>`;
        document.body.appendChild(root);
        const bar = root.querySelector(".bar > div");
        const doneLine = root.querySelector(".done-line");
        const setStep = (state, n) => {
            const li = root.querySelector(`li[data-step="${n}"]`);
            if (li) { li.classList.remove("active","done"); li.classList.add(state); }
        };
        const setProgress = (p) => { bar.style.width = Math.max(0, Math.min(100, p)) + "%"; };
        const done = (status, link) => {
            doneLine.innerHTML = `状态：<b>${escapeHtml(status)}</b>${link ? ` · <a href="${link}">查看</a>` : ""} · <a href="#" data-close>关闭</a>`;
            doneLine.querySelector("[data-close]").onclick = (e) => { e.preventDefault(); root.remove(); };
        };
        const fail = (msg) => {
            bar.style.background = "var(--danger)";
            doneLine.innerHTML = `失败：${escapeHtml(msg)} · <a href="#" data-close>关闭</a>`;
            doneLine.querySelector("[data-close]").onclick = (e) => { e.preventDefault(); root.remove(); };
        };
        return { setStep, setProgress, done, fail };
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, c =>
            ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }

    // —— 文件选择器（与拖拽共用 uploadOne：天然支持批量）——
    document.addEventListener("change", (e) => {
        const inp = e.target;
        if (!(inp && inp.id === "pdf-picker")) return;
        const files = Array.from(inp.files || []);
        files.forEach(f => uploadOne(f));
        inp.value = "";  // 重置以便选相同文件
    });

    // —— 库面板按钮：摄取 / 重编译 / 删除 ——
    document.addEventListener("click", async (e) => {
        const ingestBtn = e.target.closest("[data-ingest-path]");
        if (ingestBtn) {
            e.preventDefault();
            const fname = ingestBtn.getAttribute("data-ingest-path");
            if (!confirm(`确认摄取 ${fname}？`)) return;
            ingestBtn.disabled = true;
            ingestBtn.textContent = "摄取中…";
            try {
                const r = await fetch(`/api/ingest/trigger?filename=${encodeURIComponent(fname)}`,
                    {method: "POST"});
                const data = await r.json();
                if (data && data.ok) {
                    flash(`摄取完成：r.id=${data.report_id} · ${data.status}`);
                    htmx.ajax("GET", "/api/ingest/library/pane",
                        {target: "#library-pane", swap: "innerHTML"});
                    setTimeout(() => location.reload(), 600);
                } else {
                    flash(`摄取失败：${(data && data.error) || r.status}`);
                }
            } catch (err) {
                flash("网络错误：" + err.message);
            } finally {
                ingestBtn.disabled = false;
                ingestBtn.textContent = "摄取";
            }
            return;
        }

        const reBtn = e.target.closest("[data-reingest-report]");
        if (reBtn) {
            e.preventDefault();
            const rid = reBtn.getAttribute("data-reingest-report");
            if (!confirm(`确认重编译 r.id=${rid}？（不重抽 PDF，直接走 LLM）`)) return;
            reBtn.disabled = true;
            reBtn.textContent = "重编译中…";
            try {
                const r = await fetch(`/api/ingest/recompile?report_id=${encodeURIComponent(rid)}`,
                    {method: "POST"});
                const data = await r.json();
                if (data && data.ok) {
                    flash(`重编译成功：r.id=${rid} · ${data.status}`);
                    htmx.ajax("GET", "/api/ingest/library/pane",
                        {target: "#library-pane", swap: "innerHTML"});
                    setTimeout(() => location.reload(), 600);
                } else {
                    flash(`重编译失败：${(data && data.error) || r.status}`);
                }
            } catch (err) {
                flash("网络错误：" + err.message);
            } finally {
                reBtn.disabled = false;
                reBtn.textContent = "重编译";
            }
            return;
        }

        const delBtn = e.target.closest("[data-delete-report]");
        if (delBtn) {
            e.preventDefault();
            const rid = delBtn.getAttribute("data-delete-report");
            const fname = delBtn.getAttribute("data-filename") || "";
            if (!confirm(`确认删除 r.id=${rid} (${fname})？将清空关联 Pages + Citations。`)) return;
            delBtn.disabled = true;
            try {
                const r = await fetch(`/api/ingest/${encodeURIComponent(rid)}`,
                    {method: "DELETE"});
                const data = await r.json();
                if (r.ok) {
                    flash(`已删除 r.id=${rid}（${data.deleted_citations} cites · ${data.deleted_pages} pages）`);
                    htmx.ajax("GET", "/api/ingest/library/pane",
                        {target: "#library-pane", swap: "innerHTML"});
                    setTimeout(() => location.reload(), 600);
                } else {
                    flash(`删除失败：${r.status}`);
                }
            } catch (err) {
                flash("网络错误：" + err.message);
            } finally {
                delBtn.disabled = false;
            }
            return;
        }
    });
})();


// 引用悬浮气泡：hover sup.cite → 拉 /api/pages/quote
(function () {
    const pop = document.createElement("div");
    pop.className = "cite-pop";
    pop.style.display = "none";
    document.body.appendChild(pop);

    let popTimer = null;
    function findCiteEl(e) {
        return e.target.closest("a.cite-link, sup.cite");
    }
    async function show(el) {
        const file = el.dataset.file;
        const page = el.dataset.page;
        const token = el.dataset.token || "";
        if (!file || !page) return;
        const rect = el.getBoundingClientRect();
        pop.innerHTML = `<div class="pop-cite">${escapeHTML(token)}</div>
                         <div class="pop-quote">加载中…</div>
                         <div class="pop-foot muted tiny">点击跳到原文页</div>`;
        pop.style.left = Math.min(window.innerWidth - 540, rect.left + window.scrollX) + "px";
        pop.style.top = (rect.bottom + window.scrollY + 6) + "px";
        pop.style.display = "block";
        clearTimeout(popTimer);
        try {
            const r = await fetch(`/api/pages/quote?file=${encodeURIComponent(file)}&page=${page}`);
            const data = await r.json();
            if (!r.ok) {
                pop.querySelector(".pop-quote").textContent = (data.detail || "未找到该页").trim();
                return;
            }
            pop.querySelector(".pop-quote").textContent = data.quote || "(该页无文本)";
        } catch (e) {
            pop.querySelector(".pop-quote").textContent = "请求失败: " + e.message;
        }
    }
    document.addEventListener("mouseover", (e) => {
        const t = findCiteEl(e);
        if (t) show(t);
    });
    document.addEventListener("mouseout", (e) => {
        const t = findCiteEl(e);
        if (t) {
            popTimer = setTimeout(() => {
                if (!pop.matches(":hover") && !t.matches(":hover")) pop.style.display = "none";
            }, 80);
        }
    });
    pop.addEventListener("mouseleave", () => pop.style.display = "none");

    function escapeHTML(s) {
        return String(s).replace(/[&<>"']/g, c =>
            ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }
})();
