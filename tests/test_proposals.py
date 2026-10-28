"""沉淀提案提醒语义自测：GET 只读、稍后处理只推迟、不再提醒才标记。

用法：python tests/test_proposals.py
全部在临时目录里跑，不碰真实会话。
"""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import config, sessions  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


def sandbox():
    tmp = Path(tempfile.mkdtemp(prefix="proposal-test-"))
    for name in ("raw", "wiki", "data", "outputs"):
        (tmp / name).mkdir(parents=True, exist_ok=True)
    config.ROOT = tmp
    config.RAW_DIR = tmp / "raw"
    config.INBOX_DIR = config.RAW_DIR / "inbox"
    config.REPORTS_DIR = config.RAW_DIR / "reports"
    config.FILINGS_DIR = config.RAW_DIR / "filings"
    config.POLICIES_DIR = config.RAW_DIR / "policies"
    config.WIKI_DIR = tmp / "wiki"
    config.INDEX_MD = config.WIKI_DIR / "index.md"
    config.LOG_MD = config.WIKI_DIR / "log.md"
    config.DATA_DIR = tmp / "data"
    config.EXTRACTS_DIR = config.DATA_DIR / "extracts"
    config.FIN_DIR = config.DATA_DIR / "fin"
    config.MANIFEST_PATH = config.DATA_DIR / "manifest.json"
    config.OUTPUTS_DIR = tmp / "outputs"
    config.TYPE_DIRS = {
        "company": config.WIKI_DIR / "companies",
        "industry": config.WIKI_DIR / "industries",
        "concept": config.WIKI_DIR / "concepts",
        "source": config.WIKI_DIR / "sources",
    }
    for d in (config.INBOX_DIR, config.REPORTS_DIR, config.FILINGS_DIR, config.POLICIES_DIR,
              config.DATA_DIR, config.EXTRACTS_DIR, config.FIN_DIR, config.OUTPUTS_DIR,
              *config.TYPE_DIRS.values()):
        d.mkdir(parents=True, exist_ok=True)
    # sessions 的路径是模块常量，测试里显式指到沙盒并清掉内存缓存
    sessions._PATH = config.DATA_DIR / "conversations.json"
    sessions._convs = None
    sessions._WARNINGS.clear()


def main():
    sandbox()
    conv = sessions.create("测试会话")
    msg = sessions.append_message(conv["id"], {
        "role": "assistant",
        "status": "done",
        "kind": "answer",
        "content": "答案",
        "proposal": {"action": "new", "title": "测试页", "lines": ["一条新知识 [[t.pdf#p1]]"]},
    })
    path = config.DATA_DIR / "conversations.json"
    before = path.read_bytes()

    from src.web.app import app
    c = app.test_client()

    print("GET 只读：")
    a = c.get("/api/proposals/pending").get_json()["items"]
    b = c.get("/api/proposals/pending").get_json()["items"]
    check("两次都能看到提案", (len(a), len(b)), (1, 1))
    check("刷新不会清掉提案", path.read_bytes(), before)
    m = sessions.get(conv["id"])["messages"][0]
    check("没有写 presented 标记", "presented" in m, False)

    print("稍后处理：")
    r = c.post("/api/proposals/defer", json={"conv_id": conv["id"], "msg_id": msg["id"], "hours": 1})
    check("接口成功", r.status_code, 200)
    check("推迟后不再提醒", len(c.get("/api/proposals/pending").get_json()["items"]), 0)
    m = sessions.get(conv["id"])["messages"][0]
    check("提案本身没丢", bool(m.get("proposal")), True)
    check("只记了推迟时间", bool(m.get("proposal_snoozed_until")), True)

    # 把推迟时间改成过去，模拟 24 小时后再打开
    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    sessions.update_message(conv["id"], msg["id"], proposal_snoozed_until=past)
    check("到期后重新提醒", len(c.get("/api/proposals/pending").get_json()["items"]), 1)

    print("不再提醒：")
    r = c.post("/api/proposals/dismiss", json={"conv_id": conv["id"], "msg_id": msg["id"]})
    check("接口成功", r.status_code, 200)
    check("标记后不再提醒", len(c.get("/api/proposals/pending").get_json()["items"]), 0)
    m = sessions.get(conv["id"])["messages"][0]
    check("提案仍在会话里", bool(m.get("proposal")), True)
    check("明确标记已处理", m.get("proposal_dismissed"), True)

    print("会话文件损坏：")
    path.write_text("{这不是合法 JSON", encoding="utf-8")
    sessions._convs = None
    sessions._WARNINGS.clear()
    warnings = sessions.startup_warnings()
    check("启动自检有中文提示", any("无法读取" in w for w in warnings), True)
    check("提示里有备份位置", any("已备份到" in w for w in warnings), True)
    check("损坏文件本身没被删除", path.exists(), True)
    backups = list(config.DATA_DIR.glob("conversations.corrupt-*.json"))
    check("生成了损坏备份", len(backups), 1)
    check("备份内容与原文件一致", backups[0].read_text(encoding="utf-8"), "{这不是合法 JSON")
    health = c.get("/api/health").get_json()
    check("健康接口带出损坏提示", any("conversations.json" in w for w in health.get("warnings", [])), True)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {'、'.join(FAILS)}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
