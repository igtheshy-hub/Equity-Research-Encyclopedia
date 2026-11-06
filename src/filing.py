"""兼容入口：财报摄入实现已迁到 .claude/skills/finstmt-ingest/scripts/filing_pipeline.py。

保留本模块名，是为了让 src/ingest.py、tests 与 scripts/ 下的调用方继续使用
`from src import filing`。加载后把 src.filing 指向实现模块，调用方与测试里的
monkeypatch 仍作用在同一个模块对象上。维护摄取逻辑请改 skill 目录里的脚本。
"""
import importlib.util
import sys
from pathlib import Path

_PIPELINE = (Path(__file__).resolve().parents[1] / ".claude" / "skills"
             / "finstmt-ingest" / "scripts" / "filing_pipeline.py")
_spec = importlib.util.spec_from_file_location("filing_pipeline", _PIPELINE)
if _spec is None or _spec.loader is None:
    raise ImportError(f"找不到财报摄取脚本：{_PIPELINE}")
_module = importlib.util.module_from_spec(_spec)
sys.modules["filing_pipeline"] = _module
_spec.loader.exec_module(_module)
# 把 src.filing 指向实现模块：from src import filing 拿到的就是它
sys.modules[__name__] = _module
