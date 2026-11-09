"""网页路由冒烟测试：每个页面都要能打开。

之前 `/outputs` 的路由在重构时掉了（模板还在），顶部 nav 点「产出」直接 404，
但没有任何测试发现——这里把主要页面都走一遍。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import check, run_tests  # noqa: E402

# 主要页面（不需要参数、不花钱、不写数据）
PAGES = [
    "/",            # 首页
    "/kb",          # 知识库
    "/ask",         # 问答
    "/files",       # 原文件
    "/audit",       # 审校
    "/outputs",     # 产出
    "/export",      # 导出
    "/log",         # 日志
]


def test_all_pages_open():
    """主要页面都要 200，不能因为丢路由就 404。"""
    print("路由冒烟：主要页面都能打开")
    from src.web.app import app

    app.config.update(TESTING=True)
    with app.test_client() as c:
        for path in PAGES:
            r = c.get(path)
            check(f"{path} 打开正常", r.status_code, 200)


def test_nav_links_all_resolve():
    """顶部 nav 的每个链接都要有对应路由（防止再造一个 404 的入口）。"""
    print("路由冒烟：nav 链接都有路由")
    from src.web.app import app

    rules = {str(r) for r in app.url_map.iter_rules()}
    # nav 里出现的路径
    nav_paths = ["/", "/ask", "/kb", "/files", "/audit", "/outputs", "/export", "/log"]
    missing = []
    for p in nav_paths:
        hit = any(r == p or r.startswith(p + "/") or r.startswith(p + "<")
                   for r in rules)
        if not hit:
            missing.append(p)
    check("nav 链接都有路由", missing, [])


def test_outputs_route_exists():
    """outputs.html 模板在，路由也得在（曾经重构时掉过）。"""
    print("路由冒烟：/outputs 路由在")
    from src.web.app import app

    rules = {str(r) for r in app.url_map.iter_rules()}
    check("/outputs 路由已注册", "/outputs" in rules, True)


if __name__ == "__main__":
    run_tests(globals(), [
        "test_all_pages_open",
        "test_nav_links_all_resolve",
        "test_outputs_route_exists",
    ])
