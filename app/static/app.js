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
                    // 上传成功 → 立即进入"已排队"状态，不再等同步 LLM
                    ui.setStep("done", 1);
                    ui.setProgress(100);
                    // 找第一个 ok 的 report_id；若有则开始轮询
                    let reportId = null;
                    if (data && Array.isArray(data.results)) {
                        const ok = data.results.find(r => r && r.ok);
                        if (ok) reportId = ok.report_id;
                    } else if (data && data.report_id) {
                        reportId = data.report_id;
                    }
                    if (reportId) {
                        ui.bindReport(reportId);
                        startStatusPoll(reportId);
                    } else {
                        ui.done("已上传", null);
                    }
                    // 立即刷新库面板（让"未摄入"行变成"编译中"）
                    htmx.ajax("GET", "/api/ingest/library/pane",
                        {target: "#library-pane", swap: "innerHTML"});
                    // **不再** location.reload()：上传完成不会打断用户
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

    // 轮询某份 Report 的状态，4 秒一次，直到非 extracting/pending_llm_retry
    const _pollers = new Map();  // report_id → intervalId
    function startStatusPoll(reportId) {
        // 防多开
        if (_pollers.has(reportId)) return;
        const tick = async () => {
            try {
                const r = await fetch(`/api/ingest/status/${reportId}`);
                if (!r.ok) return;
                const data = await r.json();
                if (data.status === "compiled" || data.status === "failed") {
                    const card = document.querySelector(`[data-progress-report="${reportId}"]`);
                    if (card) {
                        card.querySelector(".done-line").innerHTML =
                            `状态：<b>${data.status}</b>${data.title ? ` · ${escapeHTML(data.title)}` : ""} · <a href="/audit">查看</a> · <a href="#" data-close>关闭</a>`;
                    }
                    clearInterval(_pollers.get(reportId));
                    _pollers.delete(reportId);
                    htmx.ajax("GET", "/api/ingest/library/pane",
                        {target: "#library-pane", swap: "innerHTML"});
                } else if (data.status === "pending_llm_retry") {
                    const card = document.querySelector(`[data-progress-report="${reportId}"]`);
                    if (card) {
                        card.querySelector(".done-line").innerHTML =
                            `⚠ LLM 编译失败 · ${escapeHTML((data.error || "").slice(0, 80))} · <a href="/api/ingest/recompile?report_id=${reportId}" onclick="return false">重编译</a> · <a href="#" data-close>关闭</a>`;
                        card.querySelector("[data-recompile-link]")?.addEventListener("click", () => {
                            fetch(`/api/ingest/recompile?report_id=${reportId}`, {method: "POST"}).then(r => r.json()).then(d => {
                                flash && flash(d.ok ? "重编译已启动" : "重编译失败：" + d.error);
                            });
                        });
                    }
                } else if (data.status === "extracting") {
                    const card = document.querySelector(`[data-progress-report="${reportId}"]`);
                    if (card) card.querySelector(".done-line").innerHTML =
                        `⏳ 后台编译中…（r.id=${reportId}） · <a href="#" data-close>关闭</a>`;
                }
            } catch (_) { /* 网络抖动，下一轮再试 */ }
        };
        const iv = setInterval(tick, 4000);
        _pollers.set(reportId, iv);
        // 立即跑一次不等 4 秒
        setTimeout(tick, 500);
    }
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
            <div class="done-line" style="margin-top:8px;font-size:12px;">⏳ 上传中…</div>`;
        document.body.appendChild(root);
        const bar = root.querySelector(".bar > div");
        const doneLine = root.querySelector(".done-line");
        const setStep = (_state, _n) => { /* 已无步骤条，保留接口不报错 */ };
        const setProgress = (p) => { bar.style.width = Math.max(0, Math.min(100, p)) + "%"; };
        const bindReport = (reportId) => {
            root.setAttribute("data-progress-report", String(reportId));
            doneLine.innerHTML = `✓ 已上传 · 后台编译中（r.id=${reportId}）· <a href="#" data-close>关闭</a>`;
            doneLine.querySelector("[data-close]").onclick = (e) => { e.preventDefault(); root.remove(); };
        };
        const done = (status, link) => {
            doneLine.innerHTML = `状态：<b>${escapeHtml(status)}</b>${link ? ` · <a href="${link}">查看</a>` : ""} · <a href="#" data-close>关闭</a>`;
            doneLine.querySelector("[data-close]").onclick = (e) => { e.preventDefault(); root.remove(); };
        };
        const fail = (msg) => {
            bar.style.background = "var(--danger)";
            doneLine.innerHTML = `失败：${escapeHtml(msg)} · <a href="#" data-close>关闭</a>`;
            doneLine.querySelector("[data-close]").onclick = (e) => { e.preventDefault(); root.remove(); };
        };
        return { setStep, setProgress, done, fail, bindReport };
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
            if (!confirm(`确认摄取 ${fname}？\n（已切换为异步：摄取过程在后台跑，前端不卡）`)) return;
            ingestBtn.disabled = true;
            ingestBtn.textContent = "排队中…";
            try {
                const r = await fetch(`/api/ingest/trigger?filename=${encodeURIComponent(fname)}`,
                    {method: "POST"});
                const data = await r.json();
                if (data && data.ok) {
                    flash(`已排队 r.id=${data.report_id}，后台编译中`);
                    htmx.ajax("GET", "/api/ingest/library/pane",
                        {target: "#library-pane", swap: "innerHTML"});
                    // 开个轮询直到 status 终态
                    if (data.report_id && typeof startStatusPoll === "function") {
                        startStatusPoll(data.report_id);
                    }
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
            if (!confirm(`确认重编译 r.id=${rid}？\n（不重抽 PDF，直接走 LLM；异步不卡页）`)) return;
            reBtn.disabled = true;
            reBtn.textContent = "排队中…";
            try {
                const r = await fetch(`/api/ingest/recompile?report_id=${encodeURIComponent(rid)}`,
                    {method: "POST"});
                const data = await r.json();
                if (data && data.ok) {
                    flash(`已排队 r.id=${rid}，后台重编译中`);
                    htmx.ajax("GET", "/api/ingest/library/pane",
                        {target: "#library-pane", swap: "innerHTML"});
                    if (typeof startStatusPoll === "function") {
                        startStatusPoll(Number(rid));
                    }
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
            const quoteEl = pop.querySelector(".pop-quote");
            const footEl = pop.querySelector(".pop-foot");
            if (data.status === "ok") {
                quoteEl.textContent = data.quote || "(该页无文本)";
            } else if (data.status === "report_missing") {
                quoteEl.innerHTML = `<div class="warn">⚠ ${escapeHTML(data.hint || "源文件已删除")}</div>`;
                if (data.on_disk) {
                    const btn = document.createElement("button");
                    btn.className = "btn sm primary";
                    btn.style.marginTop = "8px";
                    btn.textContent = "🔄 重新摄取此 PDF";
                    btn.onclick = async (e) => {
                        e.stopPropagation();
                        btn.disabled = true;
                        btn.textContent = "摄取中…";
                        try {
                            const tr = await fetch(`/api/ingest/trigger?filename=${encodeURIComponent(file)}`, {method: "POST"});
                            const td = await tr.json();
                            if (td && td.ok) {
                                btn.textContent = "✓ 已摄取，正在编译…";
                                htmx.ajax("GET", "/api/ingest/library/pane",
                                    {target: "#library-pane", swap: "innerHTML"});
                                setTimeout(() => show(el), 1200);  // 重试 quote
                            } else {
                                btn.textContent = "摄取失败：" + ((td && td.error) || tr.status);
                            }
                        } catch (err) {
                            btn.textContent = "网络错误：" + err.message;
                        }
                    };
                    quoteEl.appendChild(btn);
                }
                footEl.textContent = data.recoverable ? "硬盘上 PDF 还在，可一键恢复" : "硬盘上也没有，请检查文件是否被挪走";
            } else if (data.status === "page_missing") {
                quoteEl.innerHTML = `<div class="warn">⚠ ${escapeHTML(data.hint || "该页不存在")}</div>`;
                if (data.report_id) {
                    footEl.innerHTML = `r.id=${data.report_id} · status=${data.report_status || "?"}`;
                }
            } else {
                quoteEl.textContent = (data.detail || data.hint || "未找到该页").trim();
            }
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
