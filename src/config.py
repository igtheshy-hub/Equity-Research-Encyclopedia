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
}

_LLM = CFG["llm"]
MODEL_PROFILES = _LLM["models"]
DEFAULT_MODEL = _LLM.get("default") or next(iter(MODEL_PROFILES))
# 各模型可省略、从 llm 段继承的公共参数。注意：模型级写 "extra_body": {} 空对象
# 也算已设置、会阻断继承（setdefault 语义），不想继承就在模型级显式写全内容。
# DeepSeek 官方端点关闭思考用的是 thinking: {"type": "disabled"}，各模型在
# config.json 里按自家 API 文档显式配置，不依赖继承。
_SHARED_LLM_KEYS = ("max_tokens", "read_timeout_s", "max_retries", "extra_body")


def _resolve_api_key(base_url):
    host = urlparse(base_url).netloc
    if host in _PROVIDER_KEY_ENV:
        name = _PROVIDER_KEY_ENV[host]
        return name, os.getenv(name) or ""
    return "LLM_API_KEY", os.getenv("LLM_API_KEY") or ""


def model_profile(name=None):
    """取模型档案：并入 llm 段的公共参数，带上该端点对应的 key。name 为空用默认模型。"""
    pid = name or DEFAULT_MODEL
    if pid not in MODEL_PROFILES:
        raise KeyError(f"config.json 的 llm.models 里没有模型 {pid}")
    prof = {"id": pid, **MODEL_PROFILES[pid]}
    for k in _SHARED_LLM_KEYS:
        prof.setdefault(k, _LLM.get(k))
    prof["key_env"], prof["key"] = _resolve_api_key(prof["base_url"])
    return prof


def model_choices():
    """网页下拉用的模型清单，顺序同 config.json，只列 key 已配置的模型。"""
    out = []
    for pid, p in MODEL_PROFILES.items():
        if not _resolve_api_key(p["base_url"])[1]:
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
    key_env, key = _resolve_api_key(base_url)
    if not key:
        return None
    return {"base_url": base_url, "model": model, "key_env": key_env, "key": key}


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
    for d in (INBOX_DIR, REPORTS_DIR, SNAPSHOT_DIR, FILINGS_DIR, POLICIES_DIR, *TYPE_DIRS.values(),
              OUTPUTS_DIR, OUTPUTS_DIR / "charts", EXTRACTS_DIR, FIN_DIR, REPORT_JOBS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def today_str():
    return datetime.now().strftime("%Y-%m-%d")
