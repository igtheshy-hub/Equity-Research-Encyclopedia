---
name: md-to-docx
description: 把 Markdown 文件转换成排版规范的 Word 文档（.docx），支持中文仿宋字体、黑色字、1.5 倍行距、首行缩进 24 磅、标题段后 1 行、标题不用斜体，并把引文编号自动转成上标。用户要求把 .md 转成 .docx、用 Markdown 生成 Word，或提到 Word、DOCX、文档转换时使用。依赖 pandoc。
allowed-tools: Bash, Read, Write, Glob
---

# Markdown 转 Word（DOCX）

用 pandoc 把 Markdown 文件转成排版规范的 Word 文档。

## 这个技能做什么

把 Markdown 转成 Word，并处理以下格式：

- 标题与文字格式（H1 到 H6、加粗、斜体、删除线）
- 有序、无序与嵌套列表
- 表格，含表头底纹与边框
- 图片，自动嵌入
- 链接与引用
- 代码块与行内代码
- 保留中文标点（。，""（）；！？）
- **引文上标**：自动把引文编号转成上标，例如 `文献1。` 转成 `文献¹。`
- **默认中文模板**：中文全部用仿宋，英文用 Times New Roman，全文字体黑色，1.5 倍行距，首行缩进两个字符（24 磅，精确对齐），标题段后 1 行（12 磅），标题不用斜体

## 环境要求

使用前环境里要装两个包。

**pandoc**（必需）：

```bash
# macOS
brew install pandoc

# Linux（Debian/Ubuntu）
sudo apt-get install pandoc

# Windows
choco install pandoc
```

**python-docx**（生成模板时需要）：

```bash
pip install python-docx
```

检查 pandoc 是否可用：

```bash
pandoc --version
```

## 操作步骤

用户要求把 Markdown 转成 Word 时：

1. **先检查 pandoc**，运行 `pandoc --version`。没装就按上面的环境要求引导用户安装，不擅自替用户安装。
2. **确认输入与输出文件**。用户没说输出文件名就问清楚，用有意义的名称，例如输入 `report.md`，输出建议 `report.docx`。
3. **运行转换脚本**：

   ```bash
   python .claude/skills/md-to-docx/convert_md_to_docx.py input.md output.docx
   ```

4. **按需加参数**：
   - `--toc` 生成目录，默认不生成
   - `--no-chinese-template` 不用中文模板
   - `--no-superscript-citations` 关闭引文编号上标
   - `--reference-doc template.docx` 用自定义模板
   - `--title`、`--author`、`--date` 写文档元数据
5. **批量转换**：

   ```bash
   python .claude/skills/md-to-docx/convert_md_to_docx.py --batch input_dir/ output_dir/
   ```

6. **核对转换结果**，告诉用户文件落在哪里。

默认行为：脚本自动使用中文模板，中文用仿宋，除非显式加 `--no-chinese-template`。

## 示例

**转换单个文件**（使用默认中文模板）：

```bash
python .claude/skills/md-to-docx/convert_md_to_docx.py "教育改革方案.md" "教育改革方案.docx"
```

**生成目录**（可选，默认不生成）：

```bash
python .claude/skills/md-to-docx/convert_md_to_docx.py report.md report.docx --toc
```

**写元数据**：

```bash
python .claude/skills/md-to-docx/convert_md_to_docx.py paper.md paper.docx \
  --title "研究报告" \
  --author "张三" \
  --date "2025-11-20"
```

**批量转换**：

```bash
python .claude/skills/md-to-docx/convert_md_to_docx.py --batch ./markdown_files/ ./word_docs/
```

**用自定义模板**：

```bash
python .claude/skills/md-to-docx/convert_md_to_docx.py doc.md doc.docx \
  --reference-doc company_template.docx
```

## 配套文件

- **README.md**：详细用法与示例
- **快速使用指南.md**：中文快速上手
- **convert_md_to_docx.py**：主转换脚本，含自动后处理
- **create_chinese_template.py**：生成自定义中文模板
- **fix_heading_fonts.py**：修正字体与间距的后处理脚本
- **chinese_template.docx**：默认中文模板（仿宋、1.5 倍行距、标题段后间距）

## 常见问题

**提示 pandoc: command not found**
按上面的环境要求用包管理器安装 pandoc。

**图片没有出现在 Word 里**
检查 Markdown 里的图片路径，按 Markdown 文件所在目录写相对路径；确认图片文件确实存在。

**表格格式错乱**
检查 Markdown 表格语法：

```markdown
| 表头一 | 表头二 |
|--------|--------|
| 单元格 | 单元格 |
```

**脚本执行报错**
确认脚本有执行权限：`chmod +x convert_md_to_docx.py`；路径统一用正斜杠。

## 中文文档排版要点

1. **编码**：Markdown 文件用 UTF-8
2. **标点**：用中文标点（。，""（）；！？）
3. **字体**：默认模板中文全部用仿宋，正文、标题、副标题一致，字体黑色
4. **行距**：全文 1.5 倍行距
5. **段落**：首行缩进两个字符（24 磅，精确对齐）
6. **标题间距**：标题段后 1 行（12 磅）
7. **标题样式**：仿宋，不用斜体
8. **自定义模板**：用 `create_chinese_template.py` 生成单位模板

## 命令行参数

| 参数 | 说明 |
|---|---|
| `input.md` | 输入 Markdown 路径 |
| `output.docx` | 输出 Word 路径 |
| `--toc` | 生成目录，默认不生成 |
| `--no-chinese-template` | 不用默认中文模板 |
| `--no-superscript-citations` | 不把引文编号转上标 |
| `--reference-doc FILE` | 用自定义 Word 模板 |
| `--title TEXT` | 文档标题元数据 |
| `--author TEXT` | 文档作者元数据 |
| `--date TEXT` | 文档日期元数据 |
| `--batch` | 批量转换模式 |
| `--pattern PATTERN` | 批量模式的文件匹配，默认 `*.md` |

## 默认版式

中文模板自动应用以下格式：

- **字体**：中文仿宋，英文 Times New Roman
- **字号**：正文 12 磅
- **字色**：全部黑色
- **行距**：全文 1.5 倍
- **首行缩进**：24 磅，即两个中文字符
- **标题段后**：12 磅，即 1 行
- **标题样式**：加粗，不用斜体
- **引文上标**：句末标点（。；）前的数字自动转上标，例如 `参考文献1。` 转成 `参考文献¹。`；只转换紧跟在汉字或字母后的 1 到 60 号数字
- **目录**：默认不生成，加 `--toc` 才生成

完整说明见 README.md。
