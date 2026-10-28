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
# 主机名不在表里、或对应变量为空时退回通用变量 LLM_API_KEY，换服务商只改 config.json 的 llm 段。
_PROVIDER_KEY_ENV = {
    "open.bigmodel.cn": "ZHIPU_API_KEY",           # 智谱
    "ark.cn-beijing.volces.com": "ARK_API_KEY",    # 火山方舟
}

_LLM = CFG["llm"]
MODEL_PROFILES = _LLM["models"]
# 各模型可省略、从 llm 段继承的公共参数
_SHARED_LLM_KEYS = ("max_tokens", "read_timeout_s", "max_retries", "extra_body")


def _resolve_api_key(base_url):
    host = urlparse(base_url).netloc
    names = ([_PROVIDER_KEY_ENV[host]] if host in _PROVIDER_KEY_ENV else []) + ["LLM_API_KEY"]
    for name in names:
        value = os.getenv(name)
        if value:
            return name, value
    return names[0], ""


def _profile_has_key(pid):
    return bool(_resolve_api_key(MODEL_PROFILES[pid]["base_url"])[1])


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
    prof["key_env"], prof["key"] = _resolve_api_key(prof["base_url"])
    return prof


def model_choices():
    """网页下拉用的模型清单，顺序同 config.json。"""
    return [{"id": pid, "label": p.get("label") or pid, "provider": p.get("provider") or ""}
            for pid, p in MODEL_PROFILES.items()]


# 默认模型的 key，selftest 等单模型场景直接取用
_DEFAULT_PROFILE = model_profile()
LLM_API_KEY_ENV, LLM_API_KEY = _DEFAULT_PROFILE["key_env"], _DEFAULT_PROFILE["key"]

RAW_DIR = ROOT / "raw"
INBOX_DIR = RAW_DIR / "inbox"
REPORTS_DIR = RAW_DIR / "reports"
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
    for d in (INBOX_DIR, REPORTS_DIR, FILINGS_DIR, POLICIES_DIR, *TYPE_DIRS.values(),
              OUTPUTS_DIR, DATA_DIR, EXTRACTS_DIR, FIN_DIR):
        d.mkdir(parents=True, exist_ok=True)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def today_str():
    return datetime.now().strftime("%Y-%m-%d")
