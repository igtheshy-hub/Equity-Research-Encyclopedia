"""研报管线回归：拿现有任务的料，跑成文→图表→审校→Word。

用法：python scripts/e2e_report.py <job_id> [节数]
只复用原任务的页面与缺口，不改原任务；新任务的报告另起文件名。
"""
import copy
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import industry_report as ir, llm  # noqa: E402


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    src = ir.load_job(sys.argv[1])
    if not src:
        raise SystemExit("任务不存在：" + sys.argv[1])
    nsec = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    job = copy.deepcopy(src)
    job["id"] = "e2e" + uuid.uuid4().hex[:7]
    job["title"] = f"{src['title']}（回归测试）"
    secs = ir._parse_outline(src.get("outline") or "")[:nsec]
    job["outline"] = "\n\n".join(
        ["# 大纲：" + job["title"]]
        + [f"## {s['heading']}\n{ir._outline_for_write(s['body'])}" for s in secs])
    job.update(report_path="", docx_path="", charts=[], review={}, status="writing",
               error="", phase="", log=[])
    ir.save_job(job)
    ir._start = lambda *a, **k: None
    print("任务", job["id"], "节数", len(secs))
    with llm.use_model(job.get("model")):
        for fn in (ir._phase_write, ir._phase_charts, ir._phase_review, ir._phase_docx):
            try:
                fn(job["id"], None)
            except Exception as e:
                print(f"{fn.__name__} 失败：{e}")
                break
            cur = ir.load_job(job["id"])
            print(f"{fn.__name__} → {cur.get('status')}")
    cur = ir.load_job(job["id"])
    print("报告：", cur.get("report_path"))
    print("Word：", cur.get("docx_path") or "（未生成）")
    print("状态：", cur.get("status"), cur.get("error") or "")


if __name__ == "__main__":
    main()
