"""报告 API 自测：任务列表、详情、续跑、404 与静态资源白名单（不触发后台任务）。

用法：python tests/test_report_api.py（临时 job 目录）
"""
import json
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import config  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


def make_job(jid):
    return {
        "id": jid, "question": "写一份乘用车行业研报", "title": "乘用车行业研报",
        "domain": "汽车", "model": None, "status": "outline_pending",
        "outline": "# 大纲：乘用车\n\n## 一、投资要点\n- 论点", "outline_path": "",
        "samples": [], "gaps": [], "pages": [], "report_path": "", "docx_path": "",
        "charts": [], "review": {}, "candidates_path": "", "candidates_count": 0,
        "error": "", "phase": "", "log": [], "created_at": config.now_iso(),
        "updated_at": config.now_iso(),
    }


def main():
    tmp = Path(tempfile.mkdtemp(prefix="report-api-test-"))
    config.REPORT_JOBS_DIR = tmp
    (tmp / "aaa111.json").write_text(json.dumps(make_job("aaa111"), ensure_ascii=False), encoding="utf-8")

    from src.web.app import app
    c = app.test_client()

    print("== 任务列表与详情 ==")
    r = c.get("/api/report/jobs")
    check("列表 200", r.status_code, 200)
    check("列表一条", len(r.json["items"]), 1)
    check("列表状态中文", r.json["items"][0]["status_label"], "待确认大纲")
    check("缺省类型为行业研报", r.json["items"][0]["kind_label"], "行业研报")
    comp = make_job("bbb222")
    comp.update({"kind": "company", "title": "吉利汽车研究报告"})
    (tmp / "bbb222.json").write_text(json.dumps(comp, ensure_ascii=False), encoding="utf-8")
    r = c.get("/api/report/jobs")
    kinds = {x["id"]: x["kind_label"] for x in r.json["items"]}
    check("公司研报类型透出", kinds.get("bbb222"), "公司研报")
    r = c.get("/api/report/aaa111")
    check("详情 200", r.status_code, 200)
    check("详情带类型标签", (r.json["kind"], r.json["kind_label"]), ("industry", "行业研报"))
    r = c.get("/api/report/bbb222")
    check("公司详情类型透出", (r.json["kind"], r.json["kind_label"]), ("company", "公司研报"))
    r = c.get("/api/report/aaa111")
    check("详情带大纲", "投资要点" in r.json["outline"], True)
    check("详情带候选空表", r.json["candidates"], [])
    check("详情带状态标签", r.json["status_label"], "待确认大纲")

    print("== 续跑 ==")
    r = c.post("/api/report/aaa111/continue")
    check("待确认不越关", (r.status_code, r.json["job"]["status"]), (200, "outline_pending"))

    print("== 404 与空操作 ==")
    check("详情不存在", c.get("/api/report/nope").status_code, 404)
    check("确认不存在", c.post("/api/report/nope/outline", json={}).status_code, 404)
    check("缺口不存在", c.post("/api/report/nope/gaps", json={}).status_code, 404)
    check("续跑不存在", c.post("/api/report/nope/continue").status_code, 404)
    check("沉淀不存在", c.post("/api/report/nope/candidates/apply", json={}).status_code, 404)
    r = c.post("/api/report/aaa111/candidates/apply", json={"candidate_ids": []})
    check("空勾选不写 wiki", (r.status_code, r.json.get("ok")), (200, 0))

    print("== 删除研报 ==")
    check("删除不存在", c.post("/api/report/nope/delete").status_code, 404)
    runjob = make_job("run333")
    runjob["status"] = "writing"
    (tmp / "run333.json").write_text(json.dumps(runjob, ensure_ascii=False), encoding="utf-8")
    check("进行中拒绝删除", c.post("/api/report/run333/delete").status_code, 400)
    r = c.post("/api/report/bbb222/delete")
    check("删除成功", (r.status_code, r.json.get("ok")), (200, True))
    check("删除后详情 404", c.get("/api/report/bbb222").status_code, 404)

    print("== 页面与静态资源 ==")
    check("/report 200", c.get("/report").status_code, 200)
    check("图表不存在 404", c.get("/outputs/charts/nope.png").status_code, 404)
    check("下载白名单 404", c.get("/outputs/download/nope.docx").status_code, 404)
    check("取数源清单 200", c.get("/api/fetch/sources").status_code, 200)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项未通过：{'、'.join(FAILS)}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
