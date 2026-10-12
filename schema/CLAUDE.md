# 投研知识库操作规范（schema）

> 这一份是 Igshy 维护的、同名规范的本地落地版。
> GitHub 原版：https://github.com/igtheshy-hub/Equity-Research-Encyclopedia/blob/main/schema/CLAUDE.md
> 本仓库的运行时仍以 SQLite 为权威（性能/查询），但每张知识页**同时**导出为 Markdown 文件给团队 git review 用。

---

## 1. 锚点格式（**重要**：本仓库已切换）

正文关键数据句尾用以下两种锚点之一：

```
[[文件名.pdf#p页码]]              PDF，按物理页号（1-based）
[[文件名.docx#para段落号]]       Word，按文档顺序的块序号
```

例：`2026 Q1 营收同比+12.5%。[[中信证券_白酒周报_20260312.pdf#p3]]`

规则：
- **文件名不含路径、不含空格、不含 `[ ] # |`**
- **文件名在 raw/ 全库内唯一**（重复文件名 SHA 不同会走「新文件名 + 序号」）
- **锚点直指 raw 原文**；HTML 端把 `[[file.pdf#pN]]` 转成可点角的徽章
- 锚点格式不对的条会被程序丢弃，**绝不写盘**（这就是 ingest 时的诚信底盘）

---

## 2. 知识页类型

四类，目录分别：

| 类型 | 目录 | 主要分节 |
| --- | --- | --- |
| `company` | `wiki/companies/` | 公司概况 / 行业格局 / 经营与财务 / 盈利预测 / 估值 / 风险提示 / 其他 |
| `industry` | `wiki/industries/` | 行业概况 / 行业格局 / 经营与财务 / 盈利预测 / 估值 / 风险提示 / 其他 |
| `concept` | `wiki/concepts/` | 定义与口径 / 方法要点 / 政策背景 / 应用示例 / 其他 |
| `source` | `wiki/sources/` | 文件名即页名（自动生成，每份材料一份） |

---

## 3. 每页 Markdown 文件结构

```markdown
---
title: 贵州茅台
type: company                              # company | industry | concept | source
sources:                                  # 这页内容引用的源文件相对 raw/ 的路径
  - raw/reports/中信证券_白酒行业_20260312.pdf
aliases:                                   # 可选：别名（匹配用）
  - 600519.SH
summary: 一句话摘要                       # 可选：用于总索引
updated: 2026-04-20
history:                                   # 每次摄入自动追一行
  - 2026-04-20 并入 中信证券_白酒行业_20260312.pdf（+12 条）
---

## 公司概况
- 茅台 2026 Q1 营收 510 亿，同比 +8.5%。[[中信证券_白酒行业_20260312.pdf#p5]]

## 行业格局
- 高端白酒进入新周期，提价节奏趋缓。[…]

## 经营与财务
- […]

## 盈利预测
## 估值
## 风险提示
## 其他
```

正文一条知识一行（`- 句尾带锚点`）；分节只允许上表内枚举。
跨页引用用 `[[页面标题]]`，指向**已存在**的页面。

---

## 4. 摄入规则

- 读 `raw/inbox/` 中的新文件 → LLM 编译 → 程序校验锚点 → 合并 / 新建对应页
- 写盘前断言：旧页锚点集合 ⊇ 新页锚点集合（**只增不减**）
- 自动生成 `wiki/sources/<文件名>.md`，正文记录材料主题、核心结论、贡献到哪些主题页
- 重复（按 SHA256）跳过
- 扫描版（无文本层）拒绝，写日志说明

> 本仓库后端实际跑 SQLite，但每次成功摄入会**同步**导出 `wiki/<type>/<slug>.md` 供团队 review。

---

## 5. 问答规则

1. 意图判别：chat（普通问答）vs report（要求生成一份成品文档）；
2. 两步问答：第一步凭索引选页（最多 8 页），第二步只用所选页全文作答；
3. **三级回退**（区别于一般 RAG）：
   - 知识库答不上 → 程序把 `raw/` 全部文件名 + 来源摘要给 LLM 挑候选（最多 5）
   - 有候选 → 让用户选 / 确认，将对 **raw 原文** 深查
   - 无候选 → 提示用户考虑全库关键词深查
4. 答案锚点只能是 raw 锚点（`[[file.pdf#p5]]` 或 `[[file.docx#paraN]]`），程序逐个校验，违规重试一次，仍违规在答案上方标"⚠ 锚点校验未通过"
5. 知识库外 / 给定材料外的回答必须明说，禁止用模型自身知识补全
6. 跨页综合出的新知识，由模型提案，网页端弹窗勾选 + 命令行 y/N 确认后写入；写入时 history 标「会话中提取」

---

## 6. 审校规则（6 类）

| 类别 | 含义 | 触发 |
| --- | --- | --- |
| **矛盾** | 同页面内两条互相否定（如相反评级） | LLM 抽取 + 程序子串回查 |
| **时点差异** | 同主题不同日期的不同预测（**正常更新**，不报警） | LLM 抽取 |
| **过期** | 知识页 `updated` 超 90 天未刷新 | 系统（默认阈值可在 `.env` 改 `WIKI_AUDIT_*`） |
| **未摄入** | `raw/` 中存在但 SHA256 不在 manifest 的文件 | 系统 |
| **内容缺口** | 主题页缺少投研该主题常用而未覆盖的信息点 | LLM 抽取 |
| **孤立** | 知识页没有任何 `[[页面标题]]` 入链 | 系统（按 wikilink 图计算） |

修复向导：审校 JSON 给可执行动作（摄入遗漏 / 合并方向 / 矛盾保留方 / 过期标注复核），人工勾选后执行；删行类操作明确绕过"锚点只增不减"断言并留痕。

---

## 7. 命令（`manage.py`）

```
python manage.py selftest               # 体检：依赖、配置、GLM/Claude 连通、库外问答回退、全库校验
python manage.py serve                  # 启网页（127.0.0.1:5000）
python manage.py ingest [--all | --file 路径] [--dry-run]
python manage.py ask "问题"             # 库外走 raw 文件候选；检出可沉淀知识时 y/N
python manage.py audit                  # 跑 6 类审校 → outputs/audit-<时间戳>.md
python manage.py validate               # 全库格式校验
python manage.py demo                   # 导入内置示例 PDF
python manage.py seed --pdf <path>      # 摄取自定义 PDF
python manage.py rebuild-fts            # 重建 FTS5 索引
```

---

## 8. 本仓库与 Igshy 主仓的差异（运行层）

- **Igshy 主仓**：Flask + Markdown 文件（git-friendly 首类公民）
- **本仓库**：FastAPI + SQLite（运行高效、查询灵活） + **每页同步导出 Markdown**（同样可 review）

取舍：保留 SQLite 是为 PDF.js / 元数据快速查询；导出 Markdown 是为了让知识页可评审。

任何改动都要：
1. `pytest tests/` 通过（34/34 是底线）
2. 任何 prompt / schema 改动 → 再跑 `pytest tests/test_adversarial.py`，三类 fixture 仍能识别
3. **不要把 `.env`**（含 API key）提交，已 gitignore
4. 重要变更 → 在 `wiki/log.md` 追一行 history
