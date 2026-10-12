// 摄入管理页：删除按钮 + 闪信提示
(function () {
  function flash(msg, kind) {
    const el = document.getElementById("ingest-flash");
    if (!el) return;
    el.textContent = msg;
    el.className = "flash " + (kind || "ok");
    el.hidden = false;
    setTimeout(() => { el.hidden = true; }, 3500);
  }

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-delete-report]");
    if (!btn) return;
    e.preventDefault();
    const fname = btn.getAttribute("data-delete-report");
    if (!fname) return;
    if (!confirm("确认删除「" + fname + "」？\n\n会清理：\n- data/manifest.json 条目\n- wiki/sources/{stem}.md\n- 主题页 sources 数组里这一项\n- 主题页正文里的 [[file#N]] 锚点")) return;
    const row = btn.closest("tr");
    btn.disabled = true;
    btn.textContent = "删除中…";
    try {
      const r = await fetch("/api/ingest/delete", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({file: fname}),
      });
      const data = await r.json();
      if (r.ok && data.ok) {
        flash("已删除：「" + data.filename + "」" +
              "（manifest:" + (data.manifest_removed ? "✓" : "—") +
              " · 来源页:" + (data.source_page_deleted ? "✓" : "—") +
              " · 主题页联动:" + data.topic_pages_touched +
              " · 锚点清理:" + data.anchors_removed + "）",
              "ok");
        if (row) row.remove();
      } else {
        flash("删除失败：" + (data.error || r.status), "err");
        btn.disabled = false;
        btn.textContent = "删除";
      }
    } catch (err) {
      flash("网络错误：" + err.message, "err");
      btn.disabled = false;
      btn.textContent = "删除";
    }
  });
})();