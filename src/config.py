import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
CONFIG_PATH = ROOT / "config.json"

load_dotenv(ENV_PATH)

CFG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

# 各服务商的 key 变量名，按 config.json 配的端点主机名自动选，多家 key 可并存于 .env。
# 主机名不在表里时退回通用变量 LLM_API_KEY；表里的服务商变量为空就算缺失，不拿别家 key 顶，
# 否则端点只会返回难懂的 401「key 格式不对」。
_PROVIDER_KEY_ENV = {
    "open.bigmodel.cn": "ZHIPU_API_KEY",           # 智谱
    "api.moonshot.cn": "MOONSHOT_API_KEY",         # Kimi / Moonshot
    "api.minimax.cn": "MINIMAX_API_KEY",           # MiniMax（国内站）
    "api.minimaxi.com": "MINIMAX_API_KEY",         # MiniMax（国际站）
    "dashscope.aliyuncs.com": "DASHSCOPE_API_KEY",  # 通义千问
}

# 网页端允许添加的模型服务商白名单。chat 端点是主流程用的 OpenAI 兼容协议，
# anthropic 端点是救火员 Claude 子进程用的 Anthropic 兼容协议；两家端点都来自
# 各厂商官方文档，添加模型时两个端点会各发一条测试请求验证连通性。
MODEL_PROVIDERS = {
    "zhipu": {
        "label": "智谱 GLM",
        "chat_base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "anthropic_base_url": "https://open.bigmodel.cn/api/anthropic",
        "key_env": "ZHIPU_API_KEY",
    },
    "deepseek": {
        "label": "DeepSeek 官方",
        "chat_base_url": "https://api.deepseek.com/chat/completions",
        "anthropic_base_url": "https://api.deepseek.com/anthropic",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "moonshot": {
        "label": "Kimi / Moonshot",
        "chat_base_url": "https://api.moonshot.cn/v1/chat/completions",
        "anthropic_base_url": "https://api.moonshot.cn/anthropic",
        "key_env": "MOONSHOT_API_KEY",
    },
    "minimax": {
        "label": "MiniMax",
        "chat_base_url": "https://api.minimax.cn/v1/chat/completions",
        "anthropic_base_url": "https://api.minimax.cn/anthropic",
        "key_env": "MINIMAX_API_KEY",
    },
    "qwen": {
        "label": "通义千问",
        "chat_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "anthropic_base_url": "https://dashscope.aliyuncs.com/apps/anthropic",
        "key_env": "DASHSCOPE_API_KEY",
    },
}

_LLM = CFG["llm"]


def _custom_models_path():
    # 用当前 ROOT 现算，测试里 redirect_config 改了 ROOT 也能跟着走
    return Path(ROOT) / "data" / "custom_models.json"


def _load_custom_models():
    try:
        raw = json.loads(_custom_models_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    models = raw.get("models") if isinstance(raw, dict) else None
    return models if isinstance(models, dict) else {}


CUSTOM_MODELS = _load_custom_models()
# 内置模型优先，手工改出来的同名自定义模型不覆盖内置档案
MODEL_PROFILES = dict(_LLM["models"])
MODEL_PROFILES.update({k: v for k, v in CUSTOM_MODELS.items() if k not in MODEL_PROFILES})
# 各模型可省略、从 llm 段继承的公共参数。注意：模型级写 "extra_body": {} 空对象
# 也算已设置、会阻断继承（setdefault 语义），不想继承就在模型级显式写全内容。
# DeepSeek 官方端点关闭思考用的是 thinking: {"type": "disabled"}，各模型在
# config.json 里按自家 API 文档显式配置，不依赖继承。
_SHARED_LLM_KEYS = ("max_tokens", "read_timeout_s", "max_retries", "extra_body")

# config.json 必填项（问题 21：以前少了某项要跑到一半才崩，这里启动就查出来）
# 路径：顶层段 → 段内必填键
REQUIRED_CONFIG = {
    "llm": ("models",),
    "temperature": (),
    "ask": ("max_pages", "history_turns"),
    "audit": ("stale_days", "orphan_min_statements"),
    "ingest": ("compile_chunk_pages", "min_chars_per_page"),
}


def validate_config():
    """检查 config.json 的必填项。返回问题列表（空 = 没问题）。

    少配一个键，以前要等到摄入跑到一半才 KeyError 崩掉，用户一脸茫然。
    现在启动时就报清楚"缺哪一段哪个键"。
    """
    problems = []
    for section, keys in REQUIRED_CONFIG.items():
        if section not in CFG:
            problems.append(f"config.json 缺整段「{section}」")
            continue
        if not isinstance(CFG[section], dict):
            problems.append(f"config.json 的「{section}」必须是对象")
            continue
        for k in keys:
            if k not in CFG[section]:
                problems.append(f"config.json 缺「{section}.{k}」")
    # llm 段的几项（models 之外）也要有，否则 model_profile 拿不到
    for k in ("max_tokens", "read_timeout_s", "max_retries"):
        if k not in _LLM:
            problems.append(f"config.json 缺「llm.{k}」")
    if not MODEL_PROFILES:
        problems.append("config.json 的「llm.models」是空的")
    if DEFAULT_MODEL not in MODEL_PROFILES:
        problems.append(f"config.json 的「llm.default」={DEFAULT_MODEL} 不在 llm.models 里")
    return problems


def _resolve_api_key(base_url):
    host = urlparse(base_url).netloc
    if host in _PROVIDER_KEY_ENV:
        name = _PROVIDER_KEY_ENV[host]
        return name, os.getenv(name) or ""
    return "LLM_API_KEY", os.getenv("LLM_API_KEY") or ""


def _key_for_profile(prof):
    """档案自带 key（网页添加模型时填的）优先，否则按端点主机名去 .env 找。"""
    if prof.get("key"):
        return "custom", str(prof["key"])
    return _resolve_api_key(prof.get("base_url") or "")


def _profile_has_key(pid):
    return bool(_key_for_profile(MODEL_PROFILES[pid])[1])


def _resolve_default_model():
    """llm.default 支持填 "auto"：按 models 顺序取第一个 .env 里配了 key 的模型。

    auto 的意义是配置文件不再随人改动——每个人只在自己 .env 填 key，网页下拉再按需
    临时切模型；不会出现协作者把默认模型改掉又忘了改回来、其他人全环节 401 的情况。
    """
    raw = str(_LLM.get("default") or "auto").strip()
    if raw != "auto":
        return raw
    for pid in MODEL_PROFILES:
        if _profile_has_key(pid):
            return pid
    return next(iter(MODEL_PROFILES))


DEFAULT_MODEL = _resolve_default_model()


def model_profile(name=None):
    """取模型档案：并入 llm 段的公共参数，带上该端点对应的 key。name 为空用默认模型。"""
    pid = name or DEFAULT_MODEL
    if pid not in MODEL_PROFILES:
        raise KeyError(f"config.json 的 llm.models 里没有模型 {pid}")
    prof = {"id": pid, **MODEL_PROFILES[pid]}
    for k in _SHARED_LLM_KEYS:
        prof.setdefault(k, _LLM.get(k))
    prof["key_env"], prof["key"] = _key_for_profile(MODEL_PROFILES[pid])
    return prof


def model_choices():
    """网页下拉用的模型清单，顺序同 config.json，只列 key 已配置的模型。"""
    out = []
    for pid, p in MODEL_PROFILES.items():
        if not _key_for_profile(p)[1]:
            continue
        out.append({"id": pid, "label": p.get("label") or pid,
                    "provider": p.get("provider") or ""})
    return out


def rescue_settings(model_name=None):
    """救火员(claude 子进程)用的 Anthropic 兼容端点，跟随任务所选模型。

    档案缺 anthropic 子段、未知模型 id、或对应 key 为空时返回 None，spawn 据此不派活。
    """
    prof = MODEL_PROFILES.get(model_name or DEFAULT_MODEL) or {}
    anth = prof.get("anthropic") or {}
    base_url = (anth.get("base_url") or "").strip()
    model = (anth.get("model") or "").strip()
    if not base_url or not model:
        return None
    if prof.get("key"):
        key_env, key = "custom", str(prof["key"])
    else:
        key_env, key = _resolve_api_key(base_url)
    if not key:
        return None
    return {"base_url": base_url, "model": model, "key_env": key_env, "key": key}


def provider_choices():
    """网页添加模型时的服务商下拉清单（只暴露名字，不暴露 key）。"""
    return [{"id": pid, "label": p["label"]} for pid, p in MODEL_PROVIDERS.items()]


def provider_key(provider_id):
    """服务商对应的 .env key；用户没填 key 时用它兜底。"""
    prov = MODEL_PROVIDERS.get(provider_id) or {}
    for env_name in (prov.get("key_env"), "LLM_API_KEY"):
        if env_name:
            val = os.getenv(env_name) or ""
            if val:
                return val
    return ""


def add_custom_model(provider, model_id, api_key="", label="", anthropic_model=""):
    """把一个网页添加的模型写进 data/custom_models.json 并立即生效。

    只有白名单服务商能加；模型 id 与内置模型重名时拒绝，避免覆盖内置档案。
    返回网页下拉用的 {id,label,provider}。
    """
    prov = MODEL_PROVIDERS.get(provider)
    if not prov:
        raise ValueError("不支持的服务商")
    model_id = str(model_id or "").strip()
    if not model_id:
        raise ValueError("模型 ID 不能为空")
    if model_id in _LLM["models"]:
        raise ValueError("模型 ID 与内置模型重名，不能覆盖内置档案")
    prof = {
        "label": str(label or "").strip() or model_id,
        "provider": prov["label"],
        "base_url": prov["chat_base_url"],
        "model": model_id,
        "provider_id": provider,
        "custom": True,
        "anthropic": {
            "base_url": prov["anthropic_base_url"],
            "model": str(anthropic_model or "").strip() or model_id,
        },
    }
    if api_key:
        prof["key"] = str(api_key).strip()

    data = {"models": _load_custom_models()}
    data["models"][model_id] = prof
    path = _custom_models_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)   # 文件里有明文 key，尽量只给当前用户读
    except OSError:
        pass
    CUSTOM_MODELS[model_id] = prof
    MODEL_PROFILES[model_id] = prof
    return {"id": model_id, "label": prof["label"], "provider": prof["provider"]}


# 默认模型的 key，selftest 等单模型场景直接取用
_DEFAULT_PROFILE = model_profile()
LLM_API_KEY_ENV, LLM_API_KEY = _DEFAULT_PROFILE["key_env"], _DEFAULT_PROFILE["key"]

RAW_DIR = ROOT / "raw"
INBOX_DIR = RAW_DIR / "inbox"
REPORTS_DIR = RAW_DIR / "reports"
SNAPSHOT_DIR = REPORTS_DIR / "数据快照"
FILINGS_DIR = RAW_DIR / "filings"
POLICIES_DIR = RAW_DIR / "policies"

WIKI_DIR = ROOT / "wiki"
INDEX_MD = WIKI_DIR / "index.md"
LOG_MD = WIKI_DIR / "log.md"
SCHEMA_DIR = ROOT / "schema"
SKILLS_DIR = ROOT / ".claude" / "skills"
OUTPUTS_DIR = ROOT / "outputs"

DATA_DIR = ROOT / "data"
EXTRACTS_DIR = DATA_DIR / "extracts"
FIN_DIR = DATA_DIR / "fin"
REPORT_JOBS_DIR = DATA_DIR / "report_jobs"
MANIFEST_PATH = DATA_DIR / "manifest.json"
LLM_LOG_PATH = DATA_DIR / "llm_log.jsonl"

TYPE_DIRS = {
    "company": WIKI_DIR / "companies",
    "industry": WIKI_DIR / "industries",
    "concept": WIKI_DIR / "concepts",
    "source": WIKI_DIR / "sources",
}

SECTION_ENUMS = {
    "company": ["公司概况", "行业格局", "经营与财务", "盈利预测", "估值", "分析观点", "风险提示", "其他"],
    "industry": ["行业概况", "行业格局", "经营与财务", "盈利预测", "估值", "风险提示", "其他"],
    "concept": ["定义与口径", "方法要点", "政策背景", "应用示例", "其他"],
}


def ensure_dirs():
    """新克隆的仓库里 wiki/、data/、outputs/ 都是空的（运行数据不入库），启动时补齐目录。"""
    for d in (INBOX_DIR, REPORTS_DIR, SNAPSHOT_DIR, FILINGS_DIR, POLICIES_DIR, *TYPE_DIRS.values(),
              OUTPUTS_DIR, OUTPUTS_DIR / "charts", DATA_DIR, EXTRACTS_DIR, FIN_DIR, REPORT_JOBS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def today_str():
    return datetime.now().strftime("%Y-%m-%d")
