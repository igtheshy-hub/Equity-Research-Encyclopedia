"""跑知识库审校，可选走本会话 DeepSeek 接口。

用法：
  python scripts/audit_run.py
  python scripts/audit_run.py --deepseek-session   # 需用户明确授权
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import audit, config, llm  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deepseek-session", action="store_true")
    args = ap.parse_args()
    model = config.DEFAULT_MODEL
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
        model = "deepseek-v4-pro"
    t0 = time.time()
    with llm.use_model(model):
        r = audit.run()
    print(f"模型 {model}，用时 {time.time() - t0:.0f}s")
    print(r["path"])
    print(r["counts"])


if __name__ == "__main__":
    main()
