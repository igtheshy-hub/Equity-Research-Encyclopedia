/* 产出页：报告生成任务卡片 + 报告列表 */

App.onTasks.push((tasks) => {
  const area = document.getElementById("report-tasks");
  if (!area) return;
  const mine = tasks.filter((t) => t.kind === "report");
  area.innerHTML = mine.map((t) => App.taskCardHTML(t)).join("");
  const anyRunning = mine.some((t) => t.status === "running" || t.status === "waiting");
  if (!anyRunning) {
    const hint = area.querySelector(".list-refresh");
    if (!hint && mine.some((t) => t.status === "done")) {
      const p = document.createElement("p");
      p.className = "muted list-refresh";
      p.innerHTML = '新报告已生成，<a href="javascript:location.reload()">刷新页面</a>可更新下方列表。';
      area.appendChild(p);
    }
  }
});
