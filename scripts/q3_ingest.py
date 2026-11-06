"""摄入 2026 年三季报：逐份落盘、失败不中断、已摄入自动跳过。

用法：
  python scripts/q3_ingest.py --model glm-5.1
  python scripts/q3_ingest.py --model glm-5.1 --only 比亚迪
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

OUT = config.DATA_DIR / "_q3_ingest.json"


def _load_results():
    if OUT.exists():
        try:
            return json.loads(OUT.read_text(encoding="utf-8"))
        except ValueError:
            return []
    return []


def _save_results(rows):
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=config.DEFAULT_MODEL)
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--deepseek-session", action="store_true",
                    help="用本会话 ANTHROPIC_AUTH_TOKEN 调 api.deepseek.com（需用户明确授权）")
    args = ap.parse_args()

    if args.deepseek_session:
        token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        if not token:
            sys.exit("缺少 ANTHROPIC_AUTH_TOKEN，无法使用会话接口")
        config.MODEL_PROFILES["deepseek-v4-pro"] = {
            "base_url": "https://api.deepseek.com/anthropic/v1/messages",
            "model": "deepseek-v4-pro",
            "max_tokens": 16384,
            "read_timeout_s": 300,
            "max_retries": 2,
        }
        os.environ["LLM_API_KEY"] = token
        args.model = "deepseek-v4-pro"

    files = sorted(config.FILINGS_DIR.glob("*/*_2026年三季报_*.pdf"))
    if args.only:
        files = [p for p in files if args.only in p.name]
    if args.limit:
        files = files[:args.limit]
    if not files:
        print("没有找到 2026 年三季报文件")
        return

    manifest = ingest.load_manifest()
    done = {(f.get("path"), f.get("sha256")) for f in manifest["files"]}
    rows = _load_results()
    seen = {r.get("file") for r in rows if r.get("status") == "ok"}

    print(f"待处理 {len(files)} 份，模型 {args.model}")
    if args.dry_run:
        for p in files:
            print(p.relative_to(config.ROOT).as_posix())
        return
    for i, p in enumerate(files, 1):
        rel = p.relative_to(config.ROOT).as_posix()
        sha = sha256_of(p)
        if (rel, sha) in done:
            print(f"[{i}/{len(files)}] {p.name} 已摄入，跳过")
            continue
        if p.name in seen:
            print(f"[{i}/{len(files)}] {p.name} 上次已处理，跳过")
            continue
        print(f"\n[{i}/{len(files)}] == {p.name} ==")
        t0 = time.time()
        try:
            with llm.use_model(args.model):
                r = ingest.ingest_one(p, manifest)
            ingest.save_manifest(manifest)
            rows.append({
                "file": p.name, "path": rel, "status": "ok" if r else "empty",
                "result": r, "seconds": round(time.time() - t0, 1),
            })
        except Exception as e:  # noqa: BLE001
            rows.append({
                "file": p.name, "path": rel, "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc()[-2000:],
                "seconds": round(time.time() - t0, 1),
            })
            print(f"  失败：{type(e).__name__}: {e}")
        _save_results(rows)

    counts = {}
    for r in rows:
        counts[r.get("status")] = counts.get(r.get("status"), 0) + 1
    print(f"\n完成：{counts}，明细见 {OUT}")


if __name__ == "__main__":
    main()
