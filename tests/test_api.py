"""src/web/api.py 的测试。

之前 18 个接口里只有 2 个被测过（/api/files 与 /api/files/delete）。
这里补上最关键的几组：

- CSRF 守卫：所有会改状态的接口都必须带 X-Requested-With，缺了就 403
- 参数校验：空问题、空文件、非法模型名 → 400 而不是 500
- 会话增删查的基本闭环
- 任务查询

不测需要真调 LLM 的路径（那些用 stub）。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _common import api_client, check, run_tests  # noqa: E402

CSRF_HEADER = {"X-Requested-With": "wiki"}


def _client(csrf: bool = True):
    from src.web.app import app
    app.config.update(TESTING=True)
    return api_client(app, csrf=csrf)


# ── CSRF 守卫 ──────────────────────────────────────────────────────────────
def test_csrf_guard_covers_state_changing_endpoints():
    """会改状态的接口，没有同源头一律 403。"""
    print("api：CSRF 守卫覆盖所有改状态的接口")
    c = _client(csrf=False)          # 故意不带同源头

    # (路径, 请求体)
    cases = [
        ("/api/ask", {"question": "测试"}),
        ("/api/deep", {"question": "测试"}),
        ("/api/upload", {}),
        ("/api/files/delete", {"files": ["x.pdf"]}),
        ("/api/audit/run", {}),
        ("/api/audit/fix", {"actions": []}),
        ("/api/distill/apply", {"applies": []}),
        ("/api/distill/propose", {}),
        ("/api/conversations", {}),
    ]
    for path, body in cases:
        r = c.post(path, json=body)                 # 不带同源头
        check(f"{path} 缺同源头被拒", r.status_code, 403)


def test_csrf_allows_same_origin():
    """带同源头就放行（不是把功能一起挡了）。"""
    print("api：带同源头放行")
    c = _client()
    r = c.post("/api/ask", json={"question": "你好"}, headers=CSRF_HEADER)
    check("问答接口放行", r.status_code == 200 or r.status_code == 202, True)


def test_readonly_endpoints_need_no_header():
    """只读接口不该被 CSRF 守卫误伤。"""
    print("api：只读接口不受 CSRF 影响")
    c = _client()
    for path in ("/api/tasks", "/api/files", "/api/conversations", "/api/proposals/pending"):
        r = c.get(path)
        check(f"{path} 正常返回", r.status_code, 200)


# ── 参数校验 ───────────────────────────────────────────────────────────────
def test_ask_rejects_empty_question():
    """空问题要 400，不要 500。"""
    print("api：空问题返回 400")
    c = _client()
    r = c.post("/api/ask", json={"question": "   "}, headers=CSRF_HEADER)
    check("空问题 400", r.status_code, 400)
    check("带错误说明", "error" in (r.get_json() or {}), True)


def test_upload_rejects_no_files():
    """没传文件要 400。"""
    print("api：上传没文件返回 400")
    c = _client()
    r = c.post("/api/upload", json={}, headers=CSRF_HEADER)
    check("没文件 400", r.status_code, 400)


def test_upload_rejects_non_pdf():
    """非 PDF/Word 文件要被拒。"""
    print("api：上传非 PDF 被拒")
    import io
    c = _client()
    r = c.post("/api/upload",
                data={"files": (io.BytesIO(b"hello"), "note.txt")},
                headers=CSRF_HEADER)
    check("非 PDF 400", r.status_code, 400)


def test_unknown_model_rejected():
    """不存在的模型名要 400，不能悄悄用默认模型烧钱。"""
    print("api：未知模型名 400")
    c = _client()
    r = c.post("/api/ask", json={"question": "测试", "model": "不存在的模型"},
                headers=CSRF_HEADER)
    check("未知模型 400", r.status_code, 400)


def test_files_delete_requires_selection():
    """没选文件要 400。"""
    print("api：删除没选文件 400")
    c = _client()
    r = c.post("/api/files/delete", json={"files": []}, headers=CSRF_HEADER)
    check("空选择 400", r.status_code, 400)


def test_audit_fix_requires_actions():
    """修复向导没勾动作要 400。"""
    print("api：修复没动作 400")
    c = _client()
    r = c.post("/api/audit/fix", json={"actions": []}, headers=CSRF_HEADER)
    check("空动作 400", r.status_code, 400)


def test_distill_apply_requires_applies():
    """沉淀没勾行：返回空结果即可，不该 500。"""
    print("api：沉淀没动作不崩")
    c = _client()
    r = c.post("/api/distill/apply", json={"applies": []}, headers=CSRF_HEADER)
    check("不 500", r.status_code < 500, True)
    check("返回空结果", (r.get_json() or {}).get("results"), [])


# ── 会话闭环 ───────────────────────────────────────────────────────────────
def test_conversation_lifecycle():
    """会话的增 / 查 / 列表闭环。"""
    print("api：会话增查列表")
    c = _client()

    r = c.post("/api/conversations", json={}, headers=CSRF_HEADER)
    check("建会话 200", r.status_code, 200)
    conv = (r.get_json() or {}).get("conv") or {}
    cid = conv.get("id")
    check("拿到会话 id", bool(cid), True)
    if not cid:
        return

    r2 = c.get(f"/api/conversations/{cid}")
    check("读会话 200", r2.status_code, 200)
    check("会话 id 对得上", (r2.get_json() or {}).get("id"), cid)

    r3 = c.get("/api/conversations")
    check("列会话 200", r3.status_code, 200)
    items = (r3.get_json() or {}).get("items") or []
    check("列表里能查到", any(i.get("id") == cid for i in items), True)

    r4 = c.get("/api/conversations/不存在的id")
    check("读不存在的会话 404", r4.status_code, 404)


def test_deep_requires_message():
    """深查要带 msg_id，不带要 404 而不是 500。"""
    print("api：深查缺 msg_id 404")
    c = _client()
    r = c.post("/api/deep", json={"question": "测试"}, headers=CSRF_HEADER)
    check("缺 msg_id 404", r.status_code, 404)


# ── 任务 ───────────────────────────────────────────────────────────────────
def test_tasks_endpoint():
    """任务列表接口。"""
    print("api：任务列表")
    c = _client()
    r = c.get("/api/tasks")
    check("任务列表 200", r.status_code, 200)
    data = r.get_json() or {}
    check("有 tasks 字段", "tasks" in data, True)
    check("有 active 字段", "active" in data, True)


def test_task_ack_unknown_id():
    """确认一个不存在的任务不该 500。"""
    print("api：确认不存在的任务不崩")
    c = _client()
    r = c.post("/api/tasks/不存在的任务id/ack", json={}, headers=CSRF_HEADER)
    check("不 500", r.status_code < 500, True)


if __name__ == "__main__":
    run_tests(globals(), [
        "test_csrf_guard_covers_state_changing_endpoints",
        "test_csrf_allows_same_origin",
        "test_readonly_endpoints_need_no_header",
        "test_ask_rejects_empty_question",
        "test_upload_rejects_no_files",
        "test_upload_rejects_non_pdf",
        "test_unknown_model_rejected",
        "test_files_delete_requires_selection",
        "test_audit_fix_requires_actions",
        "test_distill_apply_requires_applies",
        "test_conversation_lifecycle",
        "test_deep_requires_message",
        "test_tasks_endpoint",
        "test_task_ack_unknown_id",
    ])
