"""对抗样本：把人类最容易踩的坑写成数据，让 pytest 自动跑。

三类：
- contradictions.json   → 故意对立观点，期望被 audit 抓到 contradiction 且 quote 双原文命中
- out_of_scope.json     → 知识库外问题，期望 QA 答"知识库中暂无相关记录"
- cite_misalignment.json → 错位 / 错文件 / 错 quote，期望 verification 留 note

每个 fixture 都把期望值写死，便于 CI 当作回归测试。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIX_DIR = Path(__file__).parent / "adversarial"


# ---------------------------------------------------------------------------
# 工具：从 adversarial/ 目录加载
# ---------------------------------------------------------------------------
def _load(name: str) -> list[dict]:
    p = FIX_DIR / name
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# out_of_scope：QA 必须答"不知道"
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_no_llm(monkeypatch):
    """禁用 LLM：知识库外问题应直接走早返回分支，不该去调 LLM。"""
    import app.llm.router as router_mod

    state = {"called": False}

    def _fake_sync(messages, *, options=None):
        state["called"] = True
        raise AssertionError("QA 不应该调用 LLM（候选为空时应直接答'不知道'）")

    monkeypatch.setattr(router_mod, "chat_with_fallback_sync", _fake_sync)
    return state


@pytest.mark.parametrize("case", _load("out_of_scope.json"), ids=lambda c: c["id"])
def test_out_of_scope_qa(fresh_app, stub_no_llm, case):
    from app.models.db import init_db, session_scope
    from app.services.qa import ask_question

    init_db()
    with session_scope() as s:
        r = ask_question(case["question"], session=s)

    assert r.out_of_scope is True
    assert case["expected_substring"] in r.answer_md
    # 既然知识库无候选，就不该消耗 LLM 配额
    assert stub_no_llm["called"] is False


# ---------------------------------------------------------------------------
# cite_misalignment：错位引用必须被 verification 拦下
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", _load("cite_misalignment.json"), ids=lambda c: c["id"])
def test_cite_misalignment(fresh_app, stub_llm, case):
    """把 fixture 中的 page_text 注入到 Pages，再用 citation 模块跑 verify_body_md。"""
    from app.services import citation as cite_svc

    md = case["body_md"]
    # 提取 token 里的文件名 + 页码
    refs = cite_svc.extract_citations(md)
    assert refs, f"未抽出任何 token：{md}"

    r0 = refs[0]
    page_text = case.get(f"page_text_p{r0.page_no}", "") or case.get("page_text", "")

    # filename_with_typo fixture：故意**不**把错文件名注册进 resolver，
    # 这样 verify 应报"文件名未注册/无法定位"。
    if case["id"] == "filename_with_typo":
        resolver = cite_svc.FilenameResolver.build([])  # 空
    else:
        resolver = cite_svc.FilenameResolver.build(
            [(1, r0.filename, {r0.page_no: page_text})]
        )

    verified = cite_svc.verify_body_md(md, resolver=resolver)
    notes_combined = "\n".join(n for v in verified for n in v.notes)
    assert case["expected_note_substring"] in notes_combined, (
        f"期望 verification_note 里能看到「{case['expected_note_substring']}」，"
        f"实际 notes: {notes_combined!r}"
    )


# ---------------------------------------------------------------------------
# contradictions：对立观点必须被抓出，quote 双原文命中
# ---------------------------------------------------------------------------
@pytest.fixture
def contradiction_case():
    return _load("contradictions.json")


@pytest.mark.parametrize(
    "case_idx",
    list(range(len(_load("contradictions.json")))),
    ids=[c["id"] for c in _load("contradictions.json")],
)
def test_contradiction_caught_by_audit(fresh_app, stub_llm, contradiction_case, case_idx):
    """构造 LLM stub 让其产出矛盾项，验证跑后能拿到 finding，且 quote 双原文都命中。"""
    import json
    import re
    from app.models.db import init_db, session_scope
    from app.models.orm import (
        AuditFinding, KPageSection, KnowledgePage, Page, Report,
    )
    from app.services.audit import run_audit

    case = contradiction_case[case_idx]
    init_db()

    # 直接调用 citation 模块的正规解析器，规避手写 regex
    from app.services import citation as cite_svc

    refs_a = cite_svc.extract_citations(case["kpage_a_body_md"])
    refs_b = cite_svc.extract_citations(case["kpage_b_body_md"])
    assert refs_a and refs_b, f"fixture body_md 未抽到 token: {case}"
    r_a, r_b = refs_a[0], refs_b[0]
    fn_a, page_no_a = r_a.filename, r_a.page_no
    fn_b, page_no_b = r_b.filename, r_b.page_no

    with session_scope() as s:
        ra = Report(
            sha256=case["id"] + "-a", file_path="/tmp/a.pdf",
            original_filename=fn_a,
            broker="测试", title=case["kpage_a_title"], page_count=page_no_a,
            status="compiled",
        )
        rb = Report(
            sha256=case["id"] + "-b", file_path="/tmp/b.pdf",
            original_filename=fn_b,
            broker="测试", title=case["kpage_b_title"], page_count=page_no_b,
            status="compiled",
        )
        s.add_all([ra, rb]); s.flush()
        s.add_all([
            Page(report_id=ra.id, page_no=page_no_a, text=case["kpage_a_page_text"],
                 char_count=len(case["kpage_a_page_text"])),
            Page(report_id=rb.id, page_no=page_no_b, text=case["kpage_b_page_text"],
                 char_count=len(case["kpage_b_page_text"])),
        ])
        kp_a = KnowledgePage(slug=case["id"] + "-a", title=case["kpage_a_title"], topic_tags=[case["topic"]])
        kp_b = KnowledgePage(slug=case["id"] + "-b", title=case["kpage_b_title"], topic_tags=[case["topic"]])
        s.add_all([kp_a, kp_b]); s.flush()
        s.add_all([
            KPageSection(kpage_id=kp_a.id, order_idx=0, heading="",
                body_md=case["kpage_a_body_md"]),
            KPageSection(kpage_id=kp_b.id, order_idx=0, heading="",
                body_md=case["kpage_b_body_md"]),
        ])
        s.commit()

    # 注意：fn_a / fn_b / page_no_a / page_no_b 已在前面抽出
    quote_a = case["kpage_a_body_md"].split("[[")[0].rstrip("。； \t").rstrip("[")
    quote_b = case["kpage_b_body_md"].split("[[")[0].rstrip("。； \t").rstrip("[")

    # LLM stub 直接给出矛盾项（Igshy 新格式 cite）
    stub_llm["queue"].append(json.dumps({
        "contradictions": [
            {
                "topic": case["topic"],
                "claim_a": case["kpage_a_title"],
                "quote_a": quote_a,
                "cite_a": f"{fn_a}#p{page_no_a}",
                "claim_b": case["kpage_b_title"],
                "quote_b": quote_b,
                "cite_b": f"{fn_b}#p{page_no_b}",
                "severity": "high",
                "summary": f"fixture: {case['expectation']}",
            }
        ],
        "outdated": [], "uningested": [], "isolated": [],
    }, ensure_ascii=False))

    with session_scope() as s:
        run = run_audit(s)
        assert run.status == "succeeded"
        findings = s.query(AuditFinding).filter(AuditFinding.run_id == run.id).all()
        contra = [f for f in findings if f.kind == "contradiction"]
        assert len(contra) >= 1, "audit 未抓到 fixture 设计的矛盾"
        f = contra[0]
        # quote 双原文应能在 Page.text 中找到
        assert f.verification_note is None, (
            f"quote 命中不上 Page.text：{f.verification_note!r}"
        )
        assert (f.quote_a or "") and (f.quote_b or "")
