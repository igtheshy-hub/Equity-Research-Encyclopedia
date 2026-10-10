import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
CONFIG_PATH = ROOT / "config.json"

load_dotenv(ENV_PATH)
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY", "")

CFG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

RAW_DIR = ROOT / "raw"
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
MANIFEST_PATH = DATA_DIR / "manifest.json"
LLM_LOG_PATH = DATA_DIR / "llm_log.jsonl"

TYPE_DIRS = {
    "company": WIKI_DIR / "companies",
    "industry": WIKI_DIR / "industries",
    "concept": WIKI_DIR / "concepts",
    "source": WIKI_DIR / "sources",
}

SECTION_ENUMS = {
    "company": ["公司概况", "行业格局", "经营与财务", "盈利预测", "估值", "风险提示", "其他"],
    "industry": ["行业概况", "行业格局", "经营与财务", "盈利预测", "估值", "风险提示", "其他"],
    "concept": ["定义与口径", "方法要点", "政策背景", "应用示例", "其他"],
}


def ensure_dirs():
    for d in (REPORTS_DIR, FILINGS_DIR, POLICIES_DIR, *TYPE_DIRS.values(), OUTPUTS_DIR, EXTRACTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def today_str():
    return datetime.now().strftime("%Y-%m-%d")
