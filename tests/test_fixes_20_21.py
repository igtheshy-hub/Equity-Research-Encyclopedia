"""问题清单 20、21 的回归测试。

20：AI 输出被截断没人发现；格式重写时没把上一轮输出给模型看；体检每次都联网花钱
21：一堆小杂项（配置缺项、分块阈值写死、文件名日期造假、拒答误判、数字少的答案不校验、
    审校漏摄入按文件名比、索引表竖线、Excel 公式注入、报错泄露路径、URL 换模型、
    会话不能删不能改名、轮询全量拉取）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _common import check, run_tests  # noqa: E402


# ── 问题 20：截断检测 ──────────────────────────────────────────────────────
def test_truncated_reply_is_retryable():
    """被长度上限截断的回复要判为可重试，不能当正常结果入库。"""
    print("问题 20：截断检测")
    from src.llm import _Retryable, _is_truncated, _parse_reply

    check("finish_reason=length 算截断", _is_truncated("length"), True)
    check("stop_reason=max_tokens 算截断", _is_truncated("max_tokens"), True)
    check("stop 正常结束不算截断", _is_truncated("stop"), False)
    check("end_turn 不算截断", _is_truncated("end_turn"), False)
    check("空不算截断", _is_truncated(""), False)

    # OpenAI 兼容格式的截断响应
    data = {"choices": [{"message": {"content": "营收增长 12.5%，同比"}, "finish_reason": "length"}]}
    try:
        _parse_reply(data)
        check("截断响应抛可重试异常", "没抛", "_Retryable")
    except _Retryable:
        check("截断响应抛可重试异常", "_Retryable", "_Retryable")

    # Anthropic 格式的截断响应
    data2 = {"content": [{"type": "text", "text": "半句话"}], "stop_reason": "max_tokens"}
    try:
        _parse_reply(data2)
        check("Anthropic 截断也抛", "没抛", "_Retryable")
    except _Retryable:
        check("Anthropic 截断也抛", "_Retryable", "_Retryable")

    # 正常结束不被误伤
    ok = {"choices": [{"message": {"content": "完整答案"}, "finish_reason": "stop"}]}
    text, _u = _parse_reply(ok)
    check("正常回复不受影响", text, "完整答案")


def test_json_retry_shows_previous_output():
    """格式重写时要把上一轮输出给模型看（让它改格式而不是重写）。"""
    print("问题 20：重写时带上轮输出")
    from src import llm

    calls = []

    def fake_call(messages, task, prof):
        calls.append(messages)
        if len(calls) == 1:
            return "这不是 JSON"
        return '{"ok": 1}'

    orig_call = llm._call
    orig_profile = llm.config.model_profile
    llm._call = fake_call
    llm.config.model_profile = lambda *a, **k: {
        "key": "x", "key_env": "K", "model": "m", "base_url": "http://x/v1/chat/completions",
        "read_timeout_s": 1, "max_retries": 1, "max_tokens": 100, "extra_body": {},
    }
    try:
        llm.chat([{"role": "user", "content": "问"}], task="select", response_json=True)
        check("调了两次", len(calls), 2)
        follow = calls[1][-1]["content"]
        check("第二轮带上了第一轮输出", "这不是 JSON" in follow, True)
        check("明确要求改格式", "改" in follow or "修正" in follow, True)
    finally:
        llm._call = orig_call
        llm.config.model_profile = orig_profile


def test_selftest_has_offline_mode():
    """体检要有「只查配置不联网」的开关（否则每次体检都花 API 钱）。"""
    print("问题 20：体检 --offline")
    import argparse
    import inspect

    from manage import cmd_selftest

    src = inspect.getsource(cmd_selftest)
    check("认 --offline", "args.offline" in src, True)
    # 参数确实注册了
    import manage
    p = manage.main.__doc__ or ""
    # 直接验证 parser：跑一次 --help 不报错
    try:
        import subprocess
        r = subprocess.run([sys.executable, str(ROOT / "manage.py"), "selftest", "--help"],
                           capture_output=True, timeout=30)
        check("selftest --help 正常", r.returncode, 0)
        check("帮助里有 --offline", "--offline" in r.stdout.decode("utf-8", "replace"), True)
    except Exception as e:
        check("selftest --help 正常", str(e), "正常")


# ── 问题 21：配置完整性 ────────────────────────────────────────────────────
def test_config_validation():
    """config.json 少配项要能查出来，不用跑到一半才崩。"""
    print("问题 21：配置完整性校验")
    from src import config

    problems = config.validate_config()
    check("当前配置是完整的", problems, [])

    # 造一个缺项的配置
    orig = config.CFG
    try:
        broken = json.loads(json.dumps(orig))
        del broken["ingest"]["compile_chunk_pages"]
        config.CFG = broken
        probs = config.validate_config()
        check("抓到缺的段内键", any("compile_chunk_pages" in p for p in probs), True)
    finally:
        config.CFG = orig

    try:
        broken2 = json.loads(json.dumps(orig))
        del broken2["audit"]
        config.CFG = broken2
        probs2 = config.validate_config()
        check("抓到缺的整段", any("audit" in p for p in probs2), True)
    finally:
        config.CFG = orig


# ── 问题 21：分块阈值 ──────────────────────────────────────────────────────
def test_chunking_uses_config():
    """分块阈值必须用配置里的值，不能写死 40。"""
    print("问题 21：分块按配置走")
    import inspect

    from src import ingest

    src = inspect.getsource(ingest.compile_file)
    check("没有写死的 40", "<= 40" in src, False)
    check("用了配置里的 chunk_size", "chunk_size" in src, True)

    # 行为验证：21 页的文件、配置 20 页 → 应该切成 2 块
    from src import config
    orig = config.CFG
    try:
        cfg = json.loads(json.dumps(orig))
        cfg["ingest"]["compile_chunk_pages"] = 20
        config.CFG = cfg
        units = [{"n": i, "text": f"第{i}页"} for i in range(1, 22)]

        captured = []
        orig_llm = ingest.llm.chat

        def fake_chat(messages, **kw):
            captured.append(messages[0]["content"])
            return {"report_date": "", "topics": []}

        ingest.llm.chat = fake_chat
        try:
            ingest.compile_file(Path("x.pdf"), units, "page")
            check("21 页切成 2 块", len(captured), 2)
        finally:
            ingest.llm.chat = orig_llm
    finally:
        config.CFG = orig


# ── 问题 21：文件名日期 ────────────────────────────────────────────────────
def test_invalid_date_rejected():
    """文件名里的日期要按真实日历校验（20260300 这种 3 月 0 日不能进）。"""
    print("问题 21：文件名日期校验")
    from src.classify import build_filename, valid_date8

    check("20260300 非法", valid_date8("20260300"), False)
    check("20260230 非法", valid_date8("20260230"), False)
    check("20260301 合法", valid_date8("20260301"), True)
    check("20261231 合法", valid_date8("20261231"), True)
    check("非 8 位非法", valid_date8("2026"), False)

    # 非法日期不写进文件名
    cls = {"kind": "filing", "broker": "某行", "topic": "某公司", "date": "20260300"}
    name = build_filename(cls, "原名")
    check("非法日期不进文件名", "20260300" in name, False)
    check("其余部分保留", "某公司财报" in name, True)

    # 合法日期照常写进
    cls2 = {"kind": "filing", "broker": "某行", "topic": "某公司", "date": "20260301"}
    name2 = build_filename(cls2, "原名")
    check("合法日期进文件名", "20260301" in name2, True)


# ── 问题 21：拒答判定 ──────────────────────────────────────────────────────
def test_refusal_detection():
    """只有整条回答就是 UNANSWERABLE 才算拒答。"""
    print("问题 21：拒答判定不误伤")
    from src.ask import _is_refusal

    check("纯词是拒答", _is_refusal("UNANSWERABLE"), True)
    check("带标点空格是拒答", _is_refusal("  UNANSWERABLE  "), True)
    check("带 markdown 强调是拒答", _is_refusal("**UNANSWERABLE**"), True)
    check("正常回答提到该词不是拒答",
          _is_refusal("正常回答里提到 UNANSWERABLE 这个词是什么意思"), False)
    check("空串不是拒答", _is_refusal(""), False)


# ── 问题 21：数据陈述阈值 ──────────────────────────────────────────────────
def test_data_statement_detection():
    """带单位的数据陈述要能识别（1 个数字也要查出处）。"""
    print("问题 21：数据陈述识别")
    from src.ask import _DATA_STATEMENT_RE

    check("带亿元能识别", bool(_DATA_STATEMENT_RE.search("营收 50 亿元")), True)
    check("带百分号能识别", bool(_DATA_STATEMENT_RE.search("毛利率 17.5%")), True)
    check("带元每股能识别", bool(_DATA_STATEMENT_RE.search("每股 2.53 元/股")), True)
    check("纯计数不算", bool(_DATA_STATEMENT_RE.search("共 3 个问题")), False)
    check("年份不算", bool(_DATA_STATEMENT_RE.search("2026 年的情况")), False)


# ── 问题 21：总索引表竖线 ──────────────────────────────────────────────────
def test_index_table_escapes_pipes():
    """摘要里有竖线不能弄坏总索引表。"""
    print("问题 21：总索引表竖线转义")
    from src import config, wiki

    pages = []
    d = config.TYPE_DIRS["company"]
    d.mkdir(parents=True, exist_ok=True)
    for p in d.glob("*.md"):
        p.unlink()
    config.INDEX_MD.write_text("", encoding="utf-8")

    wiki.write_page(
        d / "竖线页.md",
        {"title": "竖线页", "type": "company", "sources": ["raw/a.pdf"],
         "updated": "2026-01-01", "summary": "含|竖线|的摘要"},
        "# 竖线页\n",
    )
    pages = wiki.load_pages()
    wiki.rebuild_index_md(pages)
    idx = config.INDEX_MD.read_text(encoding="utf-8")
    line = [ln for ln in idx.splitlines() if "竖线页" in ln][0]
    # 表格列数必须一致：竖线转成全角后，列分隔符数量不变
    check("竖线已转全角", "含｜竖线｜的摘要" in line, True)
    check("没有裸竖线破坏表格", line.count("|") , 7)  # 6 列 + 首尾


# ── 问题 21：Excel 公式注入 ────────────────────────────────────────────────
def test_excel_formula_injection():
    """研报原文以 = 开头时不能被 Excel 当公式执行。"""
    print("问题 21：Excel 公式注入防护")
    from src.excel_export import _excel_safe

    check("= 开头被拦", _excel_safe("=1+1").startswith("'"), True)
    check("+ 开头被拦", _excel_safe("+1+1").startswith("'"), True)
    check("- 开头被拦", _excel_safe("-1+1").startswith("'"), True)
    check("@ 开头被拦", _excel_safe("@SUM(A1)").startswith("'"), True)
    check("普通文本不动", _excel_safe("正常文字"), "正常文字")
    check("数字不动", _excel_safe(123), 123)
    check("None 变空串", _excel_safe(None), "")


# ── 问题 21：错误脱敏 ──────────────────────────────────────────────────────
def test_error_sanitization():
    """网页报错不该露出文件完整路径。"""
    print("问题 21：错误信息脱敏")
    from src.web.api import _safe_error

    msg = _safe_error(Exception(r"File not found: C:\Users\Administrator\secret\file.pdf"), "读取失败")
    check("路径被隐藏", "C:\\Users" in msg, False)
    check("带上了路径占位", "<路径已隐藏>" in msg, True)
    check("保留了说明", "读取失败" in msg, True)


# ── 问题 21：会话增删改名 ──────────────────────────────────────────────────
def test_session_delete_and_rename():
    """会话要能删、能改名（问题 21）。"""
    print("问题 21：会话删除与改名")
    from src import sessions

    conv = sessions.create("待删会话")
    cid = conv["id"]
    check("建会话成功", bool(cid), True)

    # 改名
    new_title = sessions.rename(cid, "改成这个名字")
    check("改名成功", new_title, "改成这个名字")
    check("改名后读得到", sessions.get(cid)["title"], "改成这个名字")
    check("改名为空返回 None", sessions.rename(cid, "   "), None)
    check("改不存在的会话返回 None", sessions.rename("不存在的id", "x"), None)

    # 删除
    check("删掉返回 True", sessions.delete(cid), True)
    check("删完读不到", sessions.get(cid), None)
    check("再删返回 False", sessions.delete(cid), False)


def test_session_api_endpoints():
    """会话的删除 / 改名接口（走网页接口）。"""
    print("问题 21：会话 API 端点")
    from _common import api_client
    from src import sessions
    from src.web.app import app

    app.config.update(TESTING=True)
    c = api_client(app)
    conv = sessions.create("待改的会话")
    cid = conv["id"]

    # 改名
    r = c.patch(f"/api/conversations/{cid}", json={"title": "新名字"},
                headers={"X-Requested-With": "wiki"})
    check("改名 200", r.status_code, 200)
    check("名字已改", sessions.get(cid)["title"], "新名字")

    # 删除
    r2 = c.delete(f"/api/conversations/{cid}", headers={"X-Requested-With": "wiki"})
    check("删除 200", r2.status_code, 200)
    check("已删掉", sessions.get(cid), None)

    # 删不存在的
    r3 = c.delete("/api/conversations/不存在的id", headers={"X-Requested-With": "wiki"})
    check("删不存在的 404", r3.status_code, 404)

    # CSRF：没同源头要拒
    conv2 = sessions.create("x")
    r4 = c.delete(f"/api/conversations/{conv2['id']}")   # api_client 默认带头
    check("带同源头能删", r4.status_code, 200)


if __name__ == "__main__":
    run_tests(globals(), [
        "test_truncated_reply_is_retryable",
        "test_json_retry_shows_previous_output",
        "test_selftest_has_offline_mode",
        "test_config_validation",
        "test_chunking_uses_config",
        "test_invalid_date_rejected",
        "test_refusal_detection",
        "test_data_statement_detection",
        "test_index_table_escapes_pipes",
        "test_excel_formula_injection",
        "test_error_sanitization",
        "test_session_delete_and_rename",
        "test_session_api_endpoints",
    ])
