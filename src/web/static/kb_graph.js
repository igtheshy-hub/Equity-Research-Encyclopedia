/* 知识库图谱视图：原生 Canvas 简化力导向（斥力 + 弹簧 + 阻尼），零依赖。
   只画双链引用这一种确定性关系；语义关系（上下游/竞争）程序判不了，不画。
   交互：悬停 tooltip / 单击高亮邻居 / 双击跳页 / 拖拽节点 / 行业闭包筛选。 */

(function () {
  const TYPE_COLORS = {
    company: "#2b5bd7",
    industry: "#7a3fd1",
    concept: "#1e9e62",
    source: "#b97d10",
  };
  const TYPE_LABELS = { company: "公司", industry: "行业", concept: "概念", source: "来源摘要" };

  const listBtn = document.getElementById("view-list");
  const graphBtn = document.getElementById("view-graph");
  if (!listBtn || !graphBtn) return;
  const listView = document.getElementById("list-view");
  const graphView = document.getElementById("graph-view");
  const canvas = document.getElementById("graph-canvas");
  const tooltip = document.getElementById("graph-tooltip");
  const industrySel = document.getElementById("graph-industry");
  const unresolvedEl = document.getElementById("graph-unresolved");

  let full = null;          // /api/graph 的原始数据 {nodes, links, unresolved}
  let nodes = [];           // 参与布局的节点（筛选后的子图）
  let nodeById = {};
  let links = [];
  let neighbors = {};       // id → Set(邻居 id)
  let selectedId = null;    // 单击高亮的节点
  let raf = null;
  let width = 0, height = 0, dpr = 1;
  let hoverId = null;
  let dragId = null, dragging = false, downPos = null;

  /* ---------- 视图切换 ---------- */

  function showList() {
    setView(true);
  }

  function showGraph() {
    setView(false);
    ensureData();
  }

  function setView(list) {
    listView.hidden = !list;
    graphView.hidden = list;
    listBtn.classList.toggle("active", list);
    graphBtn.classList.toggle("active", !list);
    listBtn.setAttribute("aria-pressed", String(list));
    graphBtn.setAttribute("aria-pressed", String(!list));
    if (list) stopLoop();
  }

  listBtn.addEventListener("click", showList);
  graphBtn.addEventListener("click", showGraph);

  /* ---------- 数据 ---------- */

  function ensureData() {
    if (full) { rebuild(); return; }
    fetch("/api/graph", { cache: "no-cache" })
      .then((r) => r.json())
      .then((d) => {
        full = d || { nodes: [], links: [], unresolved: 0 };
        // 行业下拉：列出全部 type=industry 页面
        (full.nodes || []).filter((n) => n.type === "industry").forEach((n) => {
          const opt = document.createElement("option");
          opt.value = n.id;
          opt.textContent = n.title;
          industrySel.appendChild(opt);
        });
        rebuild();
      })
      .catch(() => {
        if (unresolvedEl) unresolvedEl.textContent = "图谱数据加载失败";
      });
  }

  function rebuild() {
    const keep = pickClosure(industrySel.value);
    nodeById = {};
    nodes = full.nodes.filter((n) => keep.has(n.id)).map((n) => ({
      ...n,
      x: 0, y: 0, vx: 0, vy: 0,
      r: 7 + Math.sqrt(n.inbound || 0) * 3.2,   // 半径 ∝ 入链数（开方缩放，避免枢纽过大）
    }));
    nodes.forEach((n, i) => {
      nodeById[n.id] = n;
      // 按类型分区摆放初始位置，收敛更快也更整齐
      const group = Object.keys(TYPE_COLORS).indexOf(n.type);
      const angle = (i * 2.39996 + group * 1.7) % (Math.PI * 2);  // 黄金角散开
      const rad = 60 + (i % 7) * 28;
      n.x = width / 2 + Math.cos(angle) * rad;
      n.y = height / 2 + Math.sin(angle) * rad;
    });
    links = full.links.filter((l) => keep.has(l.src) && keep.has(l.dst));
    neighbors = {};
    nodes.forEach((n) => { neighbors[n.id] = new Set(); });
    links.forEach((l) => {
      if (nodeById[l.src] && nodeById[l.dst]) {
        neighbors[l.src].add(l.dst);
        neighbors[l.dst].add(l.src);
      }
    });
    selectedId = null;
    hoverId = null;
    if (unresolvedEl) {
      const u = full.unresolved || 0;
      unresolvedEl.textContent = u > 0
        ? `${u} 条无法解析的链接未显示` : "";
    }
    resizeCanvas();
    startLoop();
  }

  /* 行业筛选：只显示该行业页的引用闭包（它引用的 + 引用它的页面及其间的边） */
  function pickClosure(industryId) {
    const all = new Set(full.nodes.map((n) => n.id));
    if (!industryId || !full.links) return all;
    const keep = new Set([industryId]);
    full.links.forEach((l) => {
      if (l.src === industryId) keep.add(l.dst);
      if (l.dst === industryId) keep.add(l.src);
    });
    return keep;
  }

  industrySel.addEventListener("change", rebuild);

  /* ---------- 画布尺寸 ---------- */

  function resizeCanvas() {
    const card = canvas.parentElement;
    if (!card) return;
    dpr = window.devicePixelRatio || 1;
    width = card.clientWidth || 800;
    height = 560;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
  }

  window.addEventListener("resize", () => {
    if (graphView.hidden) return;
    resizeCanvas();
    startLoop();
  });

  /* ---------- 力导向模拟 ---------- */

  function tick() {
    const rep = 2600;       // 斥力强度
    const spring = 0.015;   // 弹簧刚度
    const rest = 120;       // 弹簧自然长度
    const damping = 0.86;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) { dx = (Math.random() - 0.5); dy = (Math.random() - 0.5); d2 = 1; }
        const d = Math.sqrt(d2);
        const f = Math.min(rep / d2, 6);
        const fx = (dx / d) * f, fy = (dy / d) * f;
        a.vx -= fx; a.vy -= fy;
        b.vx += fx; b.vy += fy;
      }
    }
    links.forEach((l) => {
      const a = nodeById[l.src], b = nodeById[l.dst];
      if (!a || !b) return;
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (d - rest) * spring;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx += fx; a.vy += fy;
      b.vx -= fx; b.vy -= fy;
    });
    nodes.forEach((n) => {
      if (n === dragTarget) return;                 // 被拖拽的节点钉在鼠标处
      n.vx += (width / 2 - n.x) * 0.008;            // 轻微向心
      n.vy += (height / 2 - n.y) * 0.008;
      n.vx *= damping; n.vy *= damping;
      n.x += n.vx; n.y += n.vy;
      const pad = 24;
      n.x = Math.max(pad, Math.min(width - pad, n.x));
      n.y = Math.max(pad, Math.min(height - pad, n.y));
    });
  }

  function energy() {
    let e = 0;
    nodes.forEach((n) => { e += n.vx * n.vx + n.vy * n.vy; });
    return e;
  }

  function startLoop() {
    stopLoop();
    const step = () => {
      if (graphView.hidden || !nodes.length) { raf = null; return; }
      tick();
      draw();
      if (energy() < 0.4 && !dragging) { raf = null; return; }  // 收敛后停帧
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
  }

  function stopLoop() {
    if (raf) { cancelAnimationFrame(raf); raf = null; }
  }

  /* ---------- 绘制 ---------- */

  function nodeColor(n) { return TYPE_COLORS[n.type] || "#5b6478"; }

  function isDim(n) {
    // 单击高亮：选中节点的直接邻居保持原样，其余淡化
    return selectedId && n.id !== selectedId && !neighbors[selectedId].has(n.id);
  }

  function draw() {
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.lineWidth = 1;
    links.forEach((l) => {
      const a = nodeById[l.src], b = nodeById[l.dst];
      if (!a || !b) return;
      const hot = selectedId && (l.src === selectedId || l.dst === selectedId);
      ctx.strokeStyle = hot ? "rgba(43, 91, 215, .75)" : "rgba(120, 132, 160, .30)";
      ctx.lineWidth = hot ? 1.8 : 1;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    });
    ctx.textAlign = "center";
    nodes.forEach((n) => {
      const dim = isDim(n);
      ctx.globalAlpha = dim ? 0.16 : 1;
      if (n.id === selectedId || n.id === hoverId) {
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r + 4, 0, Math.PI * 2);
        ctx.strokeStyle = nodeColor(n);
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      ctx.fillStyle = nodeColor(n);
      ctx.fill();
      // 库里只有 18 页时标签全部显示；节点很多时只给较大的节点配标签
      if (nodes.length <= 60 || n.r >= 12) {
        ctx.fillStyle = dim ? "rgba(90, 100, 120, .35)" : "#21263a";
        ctx.font = "12px 'Segoe UI', 'Microsoft YaHei', sans-serif";
        const t = n.title.length > 12 ? n.title.slice(0, 11) + "…" : n.title;
        ctx.fillText(t, n.x, n.y + n.r + 14);
      }
      ctx.globalAlpha = 1;
    });
  }

  /* ---------- 交互 ---------- */

  function nodeAt(x, y) {
    for (let i = nodes.length - 1; i >= 0; i--) {
      const n = nodes[i];
      const dx = x - n.x, dy = y - n.y;
      if (dx * dx + dy * dy <= (n.r + 4) * (n.r + 4)) return n;
    }
    return null;
  }

  function canvasPos(e) {
    const rect = canvas.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  let dragTarget = null;

  canvas.addEventListener("mousedown", (e) => {
    const p = canvasPos(e);
    downPos = p;
    dragging = false;
    dragTarget = nodeAt(p.x, p.y);
    if (dragTarget) e.preventDefault();
  });

  window.addEventListener("mousemove", (e) => {
    if (dragTarget) {
      const p = canvasPos(e);
      if (downPos && (Math.abs(p.x - downPos.x) > 3 || Math.abs(p.y - downPos.y) > 3)) dragging = true;
      if (dragging) {
        dragTarget.x = Math.max(24, Math.min(width - 24, p.x));
        dragTarget.y = Math.max(24, Math.min(height - 24, p.y));
        dragTarget.vx = dragTarget.vy = 0;
        startLoop();
      }
      return;
    }
    if (graphView.hidden) return;
    const p = canvasPos(e);
    const n = nodeAt(p.x, p.y);
    if ((n && n.id) !== hoverId) {
      hoverId = n ? n.id : null;
      canvas.style.cursor = n ? "pointer" : "default";
      if (raf === null) draw();
    }
    if (n) {
      tooltip.hidden = false;
      tooltip.textContent = `${n.title} · ${TYPE_LABELS[n.type] || n.type} · 入链 ${n.inbound}`;
      const card = canvas.parentElement.getBoundingClientRect();
      tooltip.style.left = Math.min(p.x + 14, card.width - tooltip.offsetWidth - 8) + "px";
      tooltip.style.top = Math.max(p.y - 34, 4) + "px";
    } else {
      tooltip.hidden = true;
    }
  });

  window.addEventListener("mouseup", () => {
    dragTarget = null;
    setTimeout(() => { dragging = false; }, 0);  // 让 click 事件先读到拖拽标记
  });

  canvas.addEventListener("click", (e) => {
    if (dragging) return;                        // 拖拽结束不算点击
    const p = canvasPos(e);
    const n = nodeAt(p.x, p.y);
    selectedId = n ? (selectedId === n.id ? null : n.id) : null;
    if (raf === null) draw();
  });

  canvas.addEventListener("dblclick", (e) => {
    const p = canvasPos(e);
    const n = nodeAt(p.x, p.y);
    if (n) window.location.href = "/page/" + encodeURIComponent(n.type) + "/" + encodeURIComponent(n.slug);
  });

  canvas.addEventListener("mouseleave", () => {
    tooltip.hidden = true;
    hoverId = null;
  });
})();
