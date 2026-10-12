# Igshy 主仓 vs 本地升级版（Research Wiki）

> 对比与升级清单
> 基于 [Equity-Research-Encyclopedia (Igshy)](https://github.com/igtheshy-hub/Equity-Research-Encyclopedia) → 本地 FastAPI + SQLite 实现

---

## 一句话总结

本仓库是 Igshy 主仓 Equity-Research-Encyclopedia 的本地增强实现。在保留 Markdown 双写 + 句句标页码的承诺下，把 Flask 全静态站换成 FastAPI + SQLite，把单一的智谱 GLM 接入换成四家 LLM 可切换，新增三类审校发现、原文对照视图、文件浏览器、批量上传、删除报告等本地化能力。当前 **38/38 测试通过**。

---

## 一、整体架构差异

| 维度 | Igshy 主仓 | 本仓库（research-wiki） |
|---|---|---|
| Web 框架 | Flask（Jinja2 模板） | FastAPI + Uvicorn + Jinja2 + HTMX |
| 数据存储 | 全部 Markdown 文件 + 轻量 JSON | SQLite 为主 + Markdown 双写 |
| 数据模型 | 无显式 schema（仅 YAML frontmatter） | SQLAlchemy 2.x ORM：Report / Page / KnowledgePage / KPageSection / Citation / QA / AuditRun / AuditFinding |
| 检索 | 纯文件名 / 字符串匹配 | SQLite FTS5 + bigram 分词；可后续挂向量 |
| PDF 解析 | pdfplumber（无 OCR 兜底） | PyMuPDF 文字层 + PaddleOCR 兜底 |
| LLM Provider | 仅 智谱 GLM（glm-4.6） | 四家可切换：Claude / OpenAI 兼容 / MiniMax / 智谱，主备自动降级 |
| 问答流程 | 单轮 prompt 直答 | 两阶段：选页 → 作答 → 逐句引用回查 |
| 审校 | 未实现自动化审计 | 四类发现：矛盾 / 过期 / 未摄入 / 孤立 / 缺口 |
| 测试 | 仅 `mock_pdf.py` | 38 个用例：citation 单元 + e2e stub + 对抗样本（矛盾 / 库外 / 错位） |
| 依赖管理 | pip 直接装 | venv + pydantic-settings（.env 配）+ APScheduler 后台调度 |
| 代码规模 | `src/` ~2140 行 + 11 个 Flask 路由 | `app/` 16 子模块 + 30+ REST 路由 + 9 个管理子命令 |

---

## 二、与 Igshy 主仓保持一致的部分

以下点严格遵循 `schema/CLAUDE.md`，不是「升级」而是「对齐」：

- 锚点格式 `[[文件名.pdf#p页码]]` / `[[文件名.docx#para段落号]]`；与 Igshy 同款正则
- YAML frontmatter（title / type / sources / updated / aliases / summary / history）
- Wiki Markdown 双写：KnowledgePage 落库后立刻 `wiki_export.write_kpage()`，可 git diff
- 三套 LLM 提示词结构：行为约定前缀 + ingest / qa_select / qa_answer / audit 四块
- 页面互引用用 `[[页面标题]]`（wikilink），转 `/find/` 前缀搜索
- Page 物理页号 1-based，与 PDF 实际页号一一对应（这是引用回查的唯一锚）

---

## 三、本仓库新增能力清单（共 11 大项）

### 1. FastAPI + SQLite 双层存储

把 Igshy 的「纯 Markdown 文件」改成「SQLite 为主、Markdown 双写」。Resulting effect：跨文档检索变成 SQL / FTS5 而非 grep；QA 历史、审计运行、Citation quote 都能直接入库回查；删除/更新是真正的 CRUD 而不是改文件。

### 2. 多 LLM Provider 接入

在 `src/llm.py` 一个智谱 HTTP 客户端之上，新增 `app/llm/{claude,openai_compat,zhipu}.py` 三套实现，`router.py` 自动主备降级。MiniMax 不支持改只需在 `.env` 切 `LLM_PROVIDER=minimax` 即可，代码不动。Claude SDK 1.x 与 OpenAI Async 客户端共用同一 `LLMOptions/Message` 协议。

### 3. PDF 抽取升级（PyMuPDF + OCR 兜底）

用 PyMuPDF（fitz）按页抽 `text_layer`，字符数 < 30 自动栅格化 150dpi 走 PaddleOCR。CJK 文字层用 `fontfile='C:\\Windows\\Fonts\\msyh.ttc'` 解决 Helvetica 不带中文的问题。`extraction_meta` 入库便于排查 OCR 误识。

### 4. 引用回查底盘（citation.py）

`Citation` 表存 `(report_id, page_no, quote, quote_hash)`。锚点出现 → 取该 token 之前最近「完整一句」做原文子串比对（严格 / 宽松 / Jaccard 三档）。未通过的不清掉，而是进审校通道并降级 severity。这是整套「诚信」的根。

### 5. 两阶段问答 + 库外早退

`qa_select` 先用 FTS5 召回 30 候选 → LLM 选页 + confidence → `qa_answer` 严格基于选定页作答。库外问题直接返回「知识库中暂无相关记录」，避免模型编造。答案逐句再次跑引用回查，未通过的 token 转 ⚠ 标注。

### 6. 四类审计 + APScheduler 后台

`app/services/audit.py` 跑 4 种发现：
- **contradiction**：共享 tag 的两段对立，quote 双方回查
- **outdated**：同主题新旧版本矛盾
- **uningested**：同主题邻接的 PDF 列表 + 主题
- **isolated**：90 天无访问无反向链接

APScheduler 每日 02:30 自动跑，也支持 `python manage.py audit` 手动触发。

### 7. 对抗样本测试

`tests/adversarial/{contradictions,out_of_scope,cite_misalignment}.json` 三套 fixture，改提示词必须重跑：
- 故意放错页码必须被拒
- 库外问题必须命中「不知道」
- 对立观点必须被 audit 抓到 contradiction 一条

### 8. 文件浏览器 /ingest

扫描 `data/reports/` 目录对照 DB 状态分类（已编译 / 等重试 / 失败 / 重复 / 未摄入 / 不可读）。对每份提供「摄取 / 重编译 / 删除」行内按钮，重复检测按 sha256 匹配（避免同名不同内容的假阳性）。`data/` 目录路径实时显示给用户以便核对。

### 9. 批量上传 + 进度卡

`POST /api/ingest/upload` 接收 `List[UploadFile]`，每份独立走 `ingest_pdf_path` → 独立 Report 行。前端 XHR 一份一请求，每份一张进度卡（上传 / 抽取+编译 / 落库 三步骤 + 进度条）。完成后自动刷新库面板 + 重载页面。

### 10. 原文对照视图 /viewer

`GET /viewer?file=X&page=N` 渲染 Page.text 全文 + 页码导航 + 跳转 chips。每个 `sup.cite` 现在是 `<a class="cite-link" href="/viewer?..."><sup>…</sup></a>`，hover 仍弹原文气泡（不破坏既有交互），点击跳到原文页并自动 `mark.cite-highlight` 高亮匹配片段。`GET /api/reports/{file}/pdf` 直返 PDF，供 PDF 阅读器嵌入打开。

### 11. 删除报告 + 重编译

`manage.py delete-report [--id N | --all [--force]]`：CASCADE 删 Citations / Pages；KPage.source_filename 联动清理（同一文件名若还有其它 compiled Report 则保留）。

`manage.py reingest --report-id N` 或 `--all`：直接拿 Page 文本重跑 `_compile_with_llm`（不重抽 PDF）。

`--all` 默认交互确认（必须显式输入 `yes`），避免误删。

---

## 四、文件结构对比

### Igshy 主仓结构

```
├── README.md
├── config.json
├── manage.py               (4 个子命令)
├── schema/CLAUDE.md
├── src/
│   ├── config.py / llm.py / ingest.py / ask.py / audit.py
│   ├── pdf_extract.py / docx_html.py / classify.py / fix.py
│   ├── report.py / sessions.py / tasks.py / validate.py / wiki.py
│   └── web/                (app.py / api.py / render.py + templates + static)
├── tests/mock_pdf.py
└── wiki/                   (companies / industries / concepts / sources + index.md + log.md)
```

### 本仓库结构

```
├── README.md / CONTRIBUTING.md / UPGRADE_NOTES.md (本文件)
├── manage.py               (12 个子命令：serve / audit / seed / reingest / delete-report / demo / selftest / validate / init-db / demo / reingest ...)
├── schema/CLAUDE.md
├── app/
│   ├── main.py / config.py / deps.py
│   ├── models/             (db.py + orm.py)
│   ├── schemas/            (Pydantic 入参出参)
│   ├── llm/                (base.py / claude.py / openai_compat.py / zhipu.py / router.py)
│   ├── prompts/            (5 份 markdown 提示词)
│   ├── services/           (ingest / pdf_extract / qa / audit / retrieval / citation / md_render / admin / library / wiki_export / audit_runner)
│   ├── routes/             (pages.py + ingest_api + qa_api + audit_api + demo_api)
│   ├── templates/          (base / index / ingest / kpage / qa / audit / viewer / _library_pane)
│   └── static/             (app.css + app.js)
├── data/
│   ├── reports/            (用户上传的 PDF 原文件)
│   ├── pdf_cache/          (解析缓存，按 sha256 分桶)
│   ├── wiki.sqlite         (主数据库)
│   └── exports/            (导出 Markdown)
├── scripts/                (rebuild_kp7.py / fix_kp7.py / build_upgrade_doc.py)
├── tests/
│   ├── test_citation.py    (引用解析 + 原文回查)
│   ├── test_e2e_stub.py    (用 stub LLM 跑端到端)
│   ├── test_adversarial.py (三类对抗样本)
│   └── adversarial/        (fixtures)
└── wiki/                   (Markdown 双写目录，可 git diff)
```

---

## 五、管理命令对照

| 功能 | Igshy 主仓 | 本仓库 |
|---|---|---|
| 起服务 | `python manage.py serve` | `python manage.py serve [--port N --reload]` |
| 立即审校 | 无 | `python manage.py audit` |
| 种子数据 | 无（server 模式上传） | `python manage.py demo` / `seed --pdf <path>` |
| 重建索引 | 无（无 FTS） | `python manage.py rebuild-fts` |
| 建表 | 无（无 SQLite） | `python manage.py init-db` |
| 体检 | `python manage.py selftest` | `python manage.py selftest` （含 LLM 连通性 + 库外早退） |
| 格式校验 | `python manage.py validate` | `python manage.py validate` |
| 删除报告 | 无 | `python manage.py delete-report [--id N \| --all [--force]]` |
| 重编译 | 无 | `python manage.py reingest --all \| --report-id N` |
| 批量摄取 | 无 | `manage.py seed --pdf X --pdf Y --pdf Z` |

---

## 六、HTTP API 对照

| 能力 | Igshy 主仓（Flask） | 本仓库（FastAPI） |
|---|---|---|
| 摄取上传 | `POST /upload`（单文件） | `POST /api/ingest/upload` 接受 `List[UploadFile]`，批量 |
| 摄入状态 | 无 | `GET /api/ingest/status/{report_id}` |
| 删除报告 | 无 | `DELETE /api/ingest/{report_id}` |
| 文件浏览器 | 无 | `GET /api/ingest/library` + `/library/pane` |
| 硬盘一键摄取 | 无 | `POST /api/ingest/trigger?filename=X` |
| 重编译 | 无 | `POST /api/ingest/recompile?report_id=N` |
| 问答 | `POST /ask` | `POST /api/qa/ask` + `/api/qa/stream` |
| 审校运行 | `POST /audit/run` | `POST /api/audit/run` |
| 引用原文 | `GET /api/pages/quote` | `GET /api/pages/quote?file=&page=` + `GET /viewer` + `GET /api/reports/{file}/pdf` |
| 知识页导出 | `GET /wiki/<type>/<slug>` | `GET /k/<slug>` + `GET /k/<slug>/export.md` |

---

## 七、测试覆盖对比

| 测试类型 | Igshy 主仓 | 本仓库 |
|---|---|---|
| 单元测试 | 无 | `tests/test_citation.py`（引用解析 + 原文回查 100% 通过才能合 main） |
| 端到端 | 无 | `tests/test_e2e_stub.py`（stub LLM 走完整 ingest/qa/audit 流程） |
| 对抗样本 | 无 | `tests/test_adversarial.py`（contradictions / out_of_scope / cite_misalignment 三类） |
| 合计 | 8（仅 mock_pdf.py） | **38 通过** |

---

## 八、关键代码示例

### 8.1 多 Provider 切换（router.py）

```python
LLMProvider = Literal['claude', 'openai_compat', 'minimax', 'zhipu']
switch(provider, model=...)  # 立刻生效

chat_with_fallback_sync()  # 自动主备降级（智谱 → Claude）
# 若主 provider 抛 Retryable 就切下一个，避免单家服务抖动挂掉整条问答
```

### 8.2 引用回查（citation.py）

```python
ANCHOR_RE = re.compile(r"\[\[([^\[\]#|]+\.(?:pdf|docx))#(p\d+|para\d+)\]\]")

def verify_citations(body_md, page_index) -> list[CitationIssue]:
    # 每条锚点：取之前最近一句做 quote
    # 三档匹配：严格子串 / 归一化空白子串 / Jaccard ≥ 0.7
    # 不通过 → 入 AuditFinding(kind='unverified', severity='low')，不删除
    ...
```

### 8.3 库外问答早退（qa.py）

```python
if not selected_pages or confidence < threshold:
    return QA(
        answer_md='知识库中暂无相关记录，建议…',
        selected_pages=[],
        citations=[],
        out_of_scope=True,
    )
```

---

## 九、已知待扩展（Igshy 主仓也未要求）

- PDF 二进制阅读器内嵌（PDF.js 或 pdfjs-dist + 服务端 range 头）
- FTS5 + bge-small-zh 向量召回的混合排序（RRF）
- 多轮会话 / 追问（QA 上下文传递）
- KPage 手动编辑 GUI（修标题 / 标签 / 单段正文）
- Wiki 全量 zip 导出（备份 / 跨机器同步）
- 审计结果邮件 / 钉钉推送
- 操作审计：当前只审计内容，不审计「谁改的」

---

## 十、本仓库运行方式

**前置**：Python 3.11+，把 `.env.example` 复制成 `.env` 填入 `LLM_PROVIDER` 与对应 API Key。

**首次**：

```bash
python -m venv .venv && .venv/Scripts/pip install -e .
cp .env.example .env   # 改 LLM_PROVIDER 与 key
python manage.py init-db
```

**日常**：

```bash
python manage.py serve               # 浏览器 → http://127.0.0.1:5000
python manage.py demo                # 跑内置示例 PDF
python manage.py audit               # 立即跑审校
python manage.py selftest            # 体检
```

**测试**：

```bash
pytest                                # 38/38 应通过
pytest tests/test_adversarial.py -v  # 改提示词后必跑
```

**附录命令**：

```bash
python manage.py rebuild-fts          # 重建 FTS5
python manage.py delete-report --id N
python manage.py delete-report --all --force
python manage.py reingest --all       # 全部 pending_llm_retry 重跑
```

---

## 十一、与 Igshy 主仓的边界声明

本仓库不是 Igshy 主仓的「替代品」，而是它的「本地化、可运行增强版」。我们坚持 `schema/CLAUDE.md §1` 的锚点格式与双写纪律，新增的能力都围绕「让 LLM 更可信、更可换、更可恢复」展开，不引入新的锚点格式或新的存储方式。

**Pull request 回 Igshy 主仓前，须**：

1. 在 `.env` 把 `LLM_PROVIDER` 切回 `zhipu`、API key 切回 Igshy 公版 key
2. 删除 `data/` 与本仓库私有的 `scripts/`
3. 确保 `pytest 38/38` 通过
4. 不引入与 `schema/CLAUDE.md` 冲突的新约定

---

— 完 —