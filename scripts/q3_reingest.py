"""重摄指定公司的 2026 年三季报：先摘掉清单条目，再走财报流程，最后回写清单。

用法：
  python scripts/q3_reingest.py 上声电子 中鼎股份 --deepseek-session
  python scripts/q3_reingest.py --auto-missing 货币资金 --deepseek-session
"""
import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, ingest, llm  # noqa: E402
from src.classify import sha256_of  # noqa: E402

OUT = config.DATA_DIR / "_q3_reingest.json"


def _companies_from_store(missing_item):
    names = []
    for f in sorted(config.FIN_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        items = (data.get("periods", {}).get("2026Q3", {}) or {}).get("items", {})
        if items and missing_item not in items:
            names.append(f.stem)
    return names


def _pdf_for(name):
    direct = list((config.FILINGS_DIR / name).glob("*_2026年三季报_*.pdf"))
    if direct:
        return direct[0]
    for p in config.FILINGS_DIR.glob("*/*_2026年三季报_*.pdf"):
        if name in p.name:
            return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("companies", nargs="*")
    ap.add_argument("--auto-missing", default="", help="扫描 fin 库缺该科目的公司")
    ap.add_argument("--deepseek-session", action="store_true")
    ap.add_argument("--model", default=config.DEFAULT_MODEL)
    args = ap.parse_args()

    if args.deepseek_session:
        token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        if not token:
            sys.exit("缺少 ANTHROPIC_AUTH_TOKEN")
        config.MODEL_PROFILES["deepseek-v4-pro"] = {
            "base_url": "https://api.deepseek.com/anthropic/v1/messages",
            "model": "deepseek-v4-pro",
            "max_tokens": 16384,
            "read_timeout_s": 300,
            "max_retries": 2,
        }
        os.environ["LLM_API_KEY"] = token
        args.model = "deepseek-v4-pro"

    names = list(args.companies)
    if args.auto_missing:
        names += _companies_from_store(args.auto_missing)
    names = list(dict.fromkeys(names))
    if not names:
        sys.exit("没有指定公司")

    manifest = ingest.load_manifest()
    results = []
    for i, name in enumerate(names, 1):
        pdf = _pdf_for(name)
        if pdf is None:
            print(f"[{i}/{len(names)}] {name} 找不到三季报，跳过")
            continue
        pdf = pdf.resolve()
        sha = sha256_of(pdf)
        old = [f for f in manifest["files"] if f.get("sha256") == sha]
        manifest["files"] = [f for f in manifest["files"] if f.get("sha256") != sha]
        print(f"[{i}/{len(names)}] == {name} ==", flush=True)
        t0 = time.time()
        try:
            with llm.use_model(args.model):
                r = ingest.ingest_one(pdf, manifest)
            ingest.save_manifest(manifest)
            results.append({"company": name, "status": "ok" if r else "empty",
                            "log": (r or {}).get("log", ""), "seconds": round(time.time() - t0, 1)})
            print("  结果：", bool(r), (r or {}).get("log", ""), flush=True)
        except Exception as e:  # noqa: BLE001
            manifest["files"].extend(old)
            ingest.save_manifest(manifest)
            results.append({"company": name, "status": "error",
                            "error": f"{type(e).__name__}: {e}",
                            "trace": traceback.format_exc()[-1500:],
                            "seconds": round(time.time() - t0, 1)})
            print(f"  失败：{type(e).__name__}: {e}", flush=True)
        OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("完成", {s: sum(1 for r in results if r["status"] == s) for s in {r["status"] for r in results}})


if __name__ == "__main__":
    main()
