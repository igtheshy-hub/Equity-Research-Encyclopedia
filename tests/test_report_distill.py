"""报告沉淀落盘自测：候选按目标页分组、只写勾选行、完成后 job 置 done。

用法：python tests/test_report_distill.py（临时目录，不碰真实 data/ 与 wiki/）
"""
import tempfile
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import config, report_distill  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="distill-test-"))
    config.REPORT_JOBS_DIR = tmp

    jobs = {"j1": {"id": "j1", "question": "写一份行业研报", "title": "整车行业研报",
                   "report_path": "outputs/report-整车-20261028.md", "status": "candidates_pending",
                   "log": []}}

    def load_job(jid):
        return dict(jobs.get(jid) or {})

    def save_job(job):
        jobs[job["id"]] = dict(job)

    cands = [
        {"id": "c1", "class": "A", "chosen": False,
         "target": {"action": "merge", "type": "industry", "slug": "整车", "title": "整车行业"},
         "distill_line": "【个人观点】判断一 [[x.pdf#p5]]（个人研报观点，2026-10-28，report-x.md）"},
        {"id": "c2", "class": "E", "chosen": False,
         "target": {"action": "merge", "type": "industry", "slug": "整车", "title": "整车行业"},
         "distill_line": "【指标】2024FY 销量 = 100万辆 (年度累计) [[x.pdf#p5]]"},
        {"id": "c3", "class": "A", "chosen": False,
         "target": {"action": "create", "type": "industry", "slug": "", "title": "新行业"},
         "distill_line": "【个人观点】判断二 [[x.pdf#p6]]（个人研报观点，2026-10-28，report-x.md）"},
    ]
    report_distill.save_candidates("j1", cands)

    calls = []
    original = report_distill.ask_mod.apply_distill

    def fake_apply(proposal, chosen_lines=None, note_prefix=""):
        calls.append((proposal["title"], len(chosen_lines or []), note_prefix))
        return {"page": proposal["title"], "action": proposal["action"], "lines": len(chosen_lines or [])}

    report_distill.ask_mod.apply_distill = fake_apply
    try:
        ret = report_distill.apply("j1", ["c1", "c2", "c3"], load_job, save_job)
    finally:
        report_distill.ask_mod.apply_distill = original

    check("全部写入计数", ret["ok"], 3)
    check("无错误", ret["errors"], [])
    check("按目标页分两组", len(calls), 2)
    check("合并组两行", calls[0], ("整车行业", 2, "报告沉淀"))
    check("新建组一行", calls[1], ("新行业", 1, "报告沉淀"))
    check("job 置 done", jobs["j1"]["status"], "done")
    stored = report_distill.load_candidates("j1")
    check("候选标记已写入", [c["chosen"] for c in stored], [True, True, True])

    print("== 只写勾选行 ==")
    jobs["j2"] = {"id": "j2", "question": "q", "title": "t", "report_path": "outputs/r.md",
                  "status": "candidates_pending", "log": []}
    report_distill.save_candidates("j2", [
        {"id": "k1", "class": "A", "chosen": False,
         "target": {"action": "create", "type": "industry", "slug": "", "title": "页一"},
         "distill_line": "【个人观点】判断一 [[x.pdf#p5]]（个人研报观点，2026-10-28，r.md）"},
        {"id": "k2", "class": "A", "chosen": False,
         "target": {"action": "create", "type": "industry", "slug": "", "title": "页二"},
         "distill_line": "【个人观点】判断二 [[x.pdf#p5]]（个人研报观点，2026-10-28，r.md）"},
    ])
    calls.clear()
    report_distill.ask_mod.apply_distill = fake_apply
    try:
        ret = report_distill.apply("j2", ["k2"], load_job, save_job)
    finally:
        report_distill.ask_mod.apply_distill = original
    check("只调一次", calls, [("页二", 1, "报告沉淀")])
    check("只写一条", ret["ok"], 1)
    stored = report_distill.load_candidates("j2")
    check("未勾选保持未写", [c["chosen"] for c in stored], [False, True])

    print()
    if FAILS:
        print(f"{len(FAILS)} 项未通过：{'、'.join(FAILS)}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
