"""SQLite FTS5 检索。

可在 KPage 标题 / 简介 / 所有 section body_md 上做全文检索。
中文按 bigram 二元切分（不依赖 jieba，便于先跑起来），jaccard/编辑距离后续再上。

主入口：
- rebuild_index(session)               重建 FTS
- search(session, query, limit=30)     返回 [(kpage_id, score, kpage_summary), ...]
"""
from __future__ import annotations

import re
import sqlite3
from typing import Iterable, List, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# FTS 虚拟表创建 / 重建
# ---------------------------------------------------------------------------
_FTS_CREATE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS kpages_fts USING fts5(
    kpage_id UNINDEXED,
    title,
    brief,
    body,
    tokenize = 'unicode61 remove_diacritics 2',
    tokenchars = '_',
    prefix = '2 3'
);
"""


def ensure_fts(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS kpages_fts USING fts5(
            kpage_id UNINDEXED,
            title,
            brief,
            body,
            tokenize = 'unicode61 remove_diacritics 2',
            tokenchars = '_',
            prefix = '2 3'
        );
        """
    )


def bigrams(q: str) -> list[str]:
    """中文 / 任意 unicode 字符串 → bigram 列表（含 2-gram 词项，便于 FTS5 prefix 查询）。"""
    q = q.strip()
    if not q:
        return []
    # 空白分词后再 bigram
    out: list[str] = []
    for tok in re.findall(r"[\w一-鿿]+", q, flags=re.UNICODE):
        if len(tok) <= 2:
            out.append(tok)
        else:
            for i in range(len(tok) - 1):
                out.append(tok[i:i + 2])
            out.append(tok)
    # 去空
    return [w for w in (s.strip() for s in out) if w]


def _to_fts_query(q: str) -> str:
    """把用户问题转成 FTS5 MATCH 表达式：bigrams 用 OR 串起来，再加原 token."""
    tokens = re.findall(r"[\w一-鿿]+", q, flags=re.UNICODE)
    bg = bigrams(q)
    parts = list({*tokens, *bg})
    if not parts:
        return ""
    quoted = []
    for p in parts:
        # 防 FTS5 解析错误：去掉引号、保留安全字符
        p = re.sub(r"[\"\'\(\)]+", "", p)
        if not p:
            continue
        quoted.append(f'"{p}"*')
    return " OR ".join(quoted)


def rebuild_index(session: Session) -> int:
    """从 SQLAlchemy session 拉出所有 KPage，重建 FTS 索引。返回条目数。"""
    from app.models.orm import KnowledgePage, KPageSection  # 避免循环

    conn = session.connection().connection  # raw sqlite3 connection
    if not isinstance(conn, sqlite3.Connection):
        # 非 SQLite 时退化为内存 bigram 匹配；MVP 阶段不支持。
        return 0

    ensure_fts(conn)
    cur = conn.cursor()
    cur.execute("DELETE FROM kpages_fts")

    rows = (
        session.query(KnowledgePage)
        .all()
    )
    n = 0
    for kp in rows:
        body = "\n".join(s.body_md for s in (kp.sections or []))
        cur.execute(
            "INSERT INTO kpages_fts(kpage_id, title, brief, body) VALUES (?, ?, ?, ?)",
            (kp.id, kp.title, kp.brief, body),
        )
        n += 1
    conn.commit()
    return n


def search(session: Session, query: str, limit: int = 30) -> List[Tuple[int, float, dict]]:
    """FTS5 召回。返回 [(kpage_id, bm25_like_score, summary_dict), ...]"""
    if not query.strip():
        return []

    conn = session.connection().connection
    if not isinstance(conn, sqlite3.Connection):
        return []

    ensure_fts(conn)
    q = _to_fts_query(query)
    if not q:
        return []

    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT kpage_id, bm25(kpages_fts) AS score, snippet(kpages_fts, 2, '<b>', '</b>', '…', 12)
            FROM kpages_fts
            WHERE kpages_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (q, limit),
        )
    except sqlite3.OperationalError as e:
        # 查询语法异常时退化为"通配 LIKE"
        return _like_fallback(session, query, limit)

    rows = cur.fetchall()
    out: list[tuple[int, float, dict]] = []
    for kpage_id, score, snip in rows:
        out.append((int(kpage_id), float(score), {"snippet": snip}))
    return out


def _like_fallback(session: Session, query: str, limit: int) -> list[tuple[int, float, dict]]:
    from app.models.orm import KnowledgePage, KPageSection

    pat = f"%{query.strip()}%"
    rows = (
        session.query(KnowledgePage)
        .filter(
            (KnowledgePage.title.ilike(pat))
            | (KnowledgePage.brief.ilike(pat))
        )
        .limit(limit)
        .all()
    )
    # 简单字面计分
    out = []
    for kp in rows:
        body = "\n".join(s.body_md for s in (kp.sections or []))
        score = -float(
            sum(1 for line in (kp.title + kp.brief + body).splitlines() if query in line)
            + 1
        )
        out.append((kp.id, score, {"snippet": kp.brief[:120]}))
    return out
