"""网页添加模型的服务商白名单、落盘、key 解析与救火端点接线。"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _common import api_client, check  # noqa: E402
from src import agent_rescue, config  # noqa: E402
from src.web import api as api_mod  # noqa: E402


def test_provider_whitelist():
    print("模型接入：服务商白名单")
    check("五家服务商", set(config.MODEL_PROVIDERS),
          {"zhipu", "deepseek", "moonshot", "minimax", "qwen"})
    for pid, p in config.MODEL_PROVIDERS.items():
        check(f"{pid} 有 chat 端点", p["chat_base_url"].startswith("https://"), True)
        check(f"{pid} 有救火端点", p["anthropic_base_url"].startswith("https://"), True)
        check(f"{pid} 有 key 变量名", bool(p["key_env"]), True)


def test_builtin_flash_has_rescue_endpoint(monkeypatch):
    print("模型接入：glm-5.3-flash 救火端点")
    monkeypatch.setattr(config, "_resolve_api_key",
                        lambda base_url: ("TEST", "sk-test"))
    rs = config.rescue_settings("glm-5.3-flash")
    check("救火端点", rs["base_url"], "https://open.bigmodel.cn/api/anthropic")
    check("救火模型名", rs["model"], "glm-5.3-flash")


def test_add_custom_model_persists_and_resolves(monkeypatch):
    print("模型接入：添加模型落盘并立即生效")
    monkeypatch.setattr(config, "MODEL_PROFILES", dict(config.MODEL_PROFILES))
    monkeypatch.setattr(config, "CUSTOM_MODELS", dict(config.CUSTOM_MODELS))
    added = config.add_custom_model("moonshot", "kimi-k2.5",
                                    api_key="sk-test", label="Kimi K2.5")
    check("返回 id", added["id"], "kimi-k2.5")
    check("返回显示名", added["label"], "Kimi K2.5")

    path = config.ROOT / "data" / "custom_models.json"
    check("已落盘", path.exists(), True)
    prof = config.MODEL_PROFILES["kimi-k2.5"]
    check("chat 端点按服务商填", prof["base_url"],
          "https://api.moonshot.cn/v1/chat/completions")
    check("救火端点按服务商填", prof["anthropic"]["base_url"],
          "https://api.moonshot.cn/anthropic")
    check("救火模型名默认同模型", prof["anthropic"]["model"], "kimi-k2.5")

    full = config.model_profile("kimi-k2.5")
    check("档案带 key", full["key"], "sk-test")
    rs = config.rescue_settings("kimi-k2.5")
    check("救火设置取到端点", rs["base_url"], "https://api.moonshot.cn/anthropic")
    check("救火设置取到 key", rs["key"], "sk-test")
    check("下拉清单出现新模型",
          "kimi-k2.5" in [m["id"] for m in config.model_choices()], True)

    raised = False
    try:
        config.add_custom_model("zhipu", "glm-5.1", api_key="x")
    except ValueError:
        raised = True
    check("内置模型不能被覆盖", raised, True)


def test_api_model_add_checks_both_protocols(monkeypatch):
    print("模型接入：双协议探活不过不保存")
    from src.web.app import app
    app.config.update(TESTING=True)
    c = api_client(app, csrf=True)
    monkeypatch.setattr(config, "provider_key", lambda pid: "sk-test")
    monkeypatch.setattr(api_mod.model_probe, "probe_chat",
                        lambda *a, **k: (False, "chat 协议失败（HTTP 401）：bad key"))
    monkeypatch.setattr(api_mod.model_probe, "probe_anthropic",
                        lambda *a, **k: (True, "救火端点可用"))
    r = c.post("/api/models", json={"provider": "moonshot", "model_id": "kimi-k2.5"})
    body = r.get_json() or {}
    check("探活失败 400", r.status_code, 400)
    check("返回 chat 失败原因", "401" in body.get("chat", ""), True)
    check("未落盘", "kimi-k2.5" not in config.MODEL_PROFILES, True)


def test_probe_endpoints(monkeypatch):
    print("模型接入：两条探活请求的 URL 与鉴权头")
    from src import model_probe
    seen = {}

    def fake_post(url, headers, payload, timeout):
        seen.update(url=url, headers=headers, payload=payload)
        return True, 200, "{}"

    monkeypatch.setattr(model_probe, "_post_json", fake_post)
    ok, _ = model_probe.probe_chat("https://x/chat", "k", "m")
    check("chat 探活通过", ok, True)
    check("chat 用 Bearer", seen["headers"]["authorization"], "Bearer k")
    ok, _ = model_probe.probe_anthropic("https://x/anthropic/", "k", "m")
    check("救火探活通过", ok, True)
    check("救火 URL 拼 /v1/messages", seen["url"], "https://x/anthropic/v1/messages")
    check("救火用 x-api-key", seen["headers"]["x-api-key"], "k")


def test_api_model_add_success(monkeypatch):
    print("模型接入：双协议通过后落盘并返回")
    from src.web.app import app
    app.config.update(TESTING=True)
    c = api_client(app, csrf=True)
    monkeypatch.setattr(config, "MODEL_PROFILES", dict(config.MODEL_PROFILES))
    monkeypatch.setattr(config, "CUSTOM_MODELS", dict(config.CUSTOM_MODELS))
    monkeypatch.setattr(api_mod.model_probe, "probe_chat",
                        lambda *a, **k: (True, "chat 协议可用"))
    monkeypatch.setattr(api_mod.model_probe, "probe_anthropic",
                        lambda *a, **k: (True, "救火端点可用"))
    r = c.post("/api/models", json={"provider": "qwen", "model_id": "qwen3-coder-plus",
                                    "api_key": "sk-test", "label": "Qwen Coder"})
    body = r.get_json() or {}
    check("添加成功 200", r.status_code, 200)
    check("返回模型", (body.get("model") or {}).get("id"), "qwen3-coder-plus")
    check("已进档案", "qwen3-coder-plus" in config.MODEL_PROFILES, True)
    check("自定义模型文件落盘", (config.ROOT / "data" / "custom_models.json").exists(), True)


def test_report_api_exposes_rescue_state(monkeypatch):
    print("模型接入：报告接口带出救火状态")
    from src.web.app import app
    app.config.update(TESTING=True)
    c = api_client(app, csrf=True)
    log = config.ROOT / "data" / "auto_rescue" / "j1.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("正在重跑成文\n", encoding="utf-8")
    fake = {
        "id": "j1", "title": "测试研报", "status": "error", "error": "boom",
        "phase": "_phase_write", "kind": "industry",
        "auto_rescue": [{
            "at": config.now_iso(), "phase": "_phase_write", "agent": "行业研报-救火员",
            "pid": 123, "log": "data/auto_rescue/j1.log",
        }],
    }
    monkeypatch.setattr(api_mod.industry_report, "load_job", lambda jid: dict(fake))
    monkeypatch.setattr(api_mod.agent_rescue, "pid_alive", lambda pid: True)
    r = c.get("/api/report/j1")
    body = r.get_json() or {}
    check("接口 200", r.status_code, 200)
    check("rescue_active", body.get("rescue_active"), True)
    check("救火活动", (body.get("rescue") or {}).get("activity"), "正在重跑成文")
    check("救火员名字", (body.get("rescue") or {}).get("agent"), "行业研报-救火员")


def test_rescue_state_activity(monkeypatch):
    print("模型接入：救火状态与日志末行")
    log = config.ROOT / "data" / "auto_rescue" / "j1.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("先读任务\n正在重跑成文\n", encoding="utf-8")
    job = {
        "status": "error", "error": "boom",
        "auto_rescue": [{
            "at": config.now_iso(), "phase": "_phase_write",
            "agent": "行业研报-救火员", "pid": 123,
            "log": "data/auto_rescue/j1.log",
        }],
    }
    monkeypatch.setattr(agent_rescue, "pid_alive", lambda pid: True)
    st = agent_rescue.rescue_state(job)
    check("救火进行中", st["active"], True)
    check("活动取日志末行", st["activity"], "正在重跑成文")
    check("错误详情带出", st["error"], "boom")
    monkeypatch.setattr(agent_rescue, "pid_alive", lambda pid: False)
    check("进程结束后不再算救火", agent_rescue.rescue_state(job)["active"], False)
