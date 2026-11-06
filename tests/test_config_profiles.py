"""模型档案与网页下拉：缺 key 的服务商不能回落到别家 key，也不进下拉。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import config  # noqa: E402


def test_known_provider_missing_key_does_not_fall_back(monkeypatch):
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "other-provider-key")
    name, key = config._resolve_api_key("https://open.bigmodel.cn/api/anthropic")
    assert name == "ZHIPU_API_KEY"
    assert key == ""


def test_model_choices_skips_models_without_key(monkeypatch):
    profiles = {
        "ok": {"label": "OK", "provider": "x",
               "base_url": "https://api.deepseek.com/chat/completions", "model": "m"},
        "no": {"label": "NO", "provider": "x",
               "base_url": "https://open.bigmodel.cn/api/anthropic", "model": "m"},
    }
    monkeypatch.setattr(config, "MODEL_PROFILES", profiles)
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    assert [c["id"] for c in config.model_choices()] == ["ok"]


def test_rescue_settings_follows_model(monkeypatch):
    profiles = {
        "glm": {"base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                "anthropic": {"base_url": "https://open.bigmodel.cn/api/anthropic",
                              "model": "glm-5.1"}},
        "ds": {"base_url": "https://api.deepseek.com/chat/completions",
               "anthropic": {"base_url": "https://api.deepseek.com/anthropic",
                             "model": "deepseek-v4-pro"}},
        "noanth": {"base_url": "https://api.deepseek.com/chat/completions"},
    }
    monkeypatch.setattr(config, "MODEL_PROFILES", profiles)
    monkeypatch.setattr(config, "DEFAULT_MODEL", "glm")
    monkeypatch.setenv("ZHIPU_API_KEY", "zk")
    monkeypatch.setenv("LLM_API_KEY", "dk")

    default = config.rescue_settings()
    assert default["base_url"] == "https://open.bigmodel.cn/api/anthropic"
    assert default["model"] == "glm-5.1"
    assert default["key_env"] == "ZHIPU_API_KEY"
    assert default["key"] == "zk"

    ds = config.rescue_settings("ds")
    assert ds["base_url"] == "https://api.deepseek.com/anthropic"
    assert ds["model"] == "deepseek-v4-pro"
    assert ds["key_env"] == "LLM_API_KEY"
    assert ds["key"] == "dk"

    assert config.rescue_settings("noanth") is None
    assert config.rescue_settings("gonemodel") is None


def test_rescue_settings_requires_key(monkeypatch):
    profiles = {"ds": {"base_url": "https://api.deepseek.com/chat/completions",
                       "anthropic": {"base_url": "https://api.deepseek.com/anthropic",
                                     "model": "deepseek-v4-pro"}}}
    monkeypatch.setattr(config, "MODEL_PROFILES", profiles)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert config.rescue_settings("ds") is None
