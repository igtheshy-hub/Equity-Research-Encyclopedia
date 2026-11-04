"""pytest 全局夹具：每个用例都跑在临时目录里，绝不碰仓库里的真实数据。

对应问题 7：删除相关的测试原来会「复制真实数据来测」，
pytest 单独跑某个用例时 `__main__` 不执行，config 还指着真实目录，
于是测试会对着仓库里真实的研报和知识页动手。这里用 autouse fixture 兜底。
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    """把 config 的全部路径指到 pytest 给的临时目录。"""
    from _common import redirect_config
    redirect_config(tmp_path)
    yield tmp_path


# 有些用例为了造假数据会**永久**替换模块级函数（如 src.wiki.load_pages、
# src.ask.load_manifest、src.llm.chat）。这些替换会漏到后面的用例里，
# 导致「单跑全绿、全量跑红」——故障随文件顺序变化，极难排查。
# 这里在每个用例前后快照并还原这些属性。
_GUARDED = [
    ("src.llm", "chat"),
    ("src.llm", "use_model"),
    ("src.wiki", "load_pages"),
    ("src.wiki", "parse_page"),
    ("src.ask", "load_manifest"),
    ("src.ask", "_index_pages"),
    ("src.ask", "_suggest_raw_files"),
    ("src.ask", "raw_file_index"),
    ("src.pdf_extract", "raw_file_index"),
]


@pytest.fixture(autouse=True)
def _restore_globals():
    """用例结束后还原被替换掉的模块级函数。"""
    import importlib

    saved = []
    for mod_name, attr in _GUARDED:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        if hasattr(mod, attr):
            saved.append((mod, attr, getattr(mod, attr)))
    yield
    for mod, attr, val in saved:
        try:
            setattr(mod, attr, val)
        except Exception:
            pass
