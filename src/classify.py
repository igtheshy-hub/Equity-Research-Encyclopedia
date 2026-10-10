import hashlib
import re
import shutil

from . import config, llm
from .pdf_extract import first_pages_text

UNSAFE = re.compile(r'[\s\[\]#|/\\:*?"<>\x00-\x1f]')

PROMPT = """你是投研资料归档员。下面是一份 PDF 文件开头几页提取出的文字。判断这份文件属于哪一类，并给出归档信息。

只输出 JSON，不要解释，格式如下：
{
  "kind": "report（卖方研报）/ filing（公司财报）/ policy（监管政策文件）三选一",
  "broker": "出具方，研报填券商或机构名，识别不出填空字符串",
  "topic": "主题：研报填行业或公司名，财报填公司名，政策填政策主题，尽量短",
  "industry": "研报所属行业（公司研报填该公司所处行业），非研报填空字符串",
  "date": "文件中的报告日期或发布日期，格式 YYYYMMDD，识别不出填空字符串",
  "reason": "一句话判断依据"
}

文件名由程序拼接（券商_主题+类型词_日期），你不用拼，把字段给准即可。
broker、topic、industry 一律用中文规范名（如 国泰君安、白酒、贵州茅台），英文材料也译成中文；证券代码等专有缩写可保留英文。

--- 文件文字开始 ---
{TEXT}
--- 文件文字结束 ---"""


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sanitize(s):
    return UNSAFE.sub("", str(s or ""))


def build_filename(cls, original_stem):
    kind_word = {"report": "研报", "filing": "财报", "policy": "政策文件"}[cls["kind"]]
    parts = []
    if sanitize(cls.get("broker")):
        parts.append(sanitize(cls["broker"]))
    topic = sanitize(cls.get("topic"))
    parts.append(f"{topic}{kind_word}" if topic else kind_word)
    date = sanitize(cls.get("date"))
    if re.fullmatch(r"\d{8}", date):
        parts.append(date)
    name = "_".join(parts) + ".pdf"
    if name == ".pdf" or set(name[:-4]) <= {"_"}:
        name = f"未归类_{sanitize(original_stem) or '文件'}.pdf"
    return name


def unique_target(directory, filename):
    if not (directory / filename).exists():
        return filename
    stem = filename[:-4]
    i = 2
    while (directory / f"{stem}-{i}.pdf").exists():
        i += 1
    return f"{stem}-{i}.pdf"


def target_directory(cls):
    if cls["kind"] == "report":
        sub = sanitize(cls.get("industry")) or sanitize(cls.get("topic")) or "未分类行业"
        return config.REPORTS_DIR / sub
    if cls["kind"] == "filing":
        sub = sanitize(cls.get("topic")) or "未分类公司"
        return config.FILINGS_DIR / sub
    return config.POLICIES_DIR


def classify_one(path):
    texts = first_pages_text(path, 3)
    content = "\n\n".join(f"[第{i}页]\n{t}" for i, t in enumerate(texts, 1) if t)
    if not content.strip():
        return {"ok": False, "file": path.name, "error": "前 3 页没有提取到文字（疑似扫描版），未归类，文件留在收件台"}
    cls = llm.chat(
        [{"role": "user", "content": PROMPT.replace("{TEXT}", content)}],
        task="classify",
        response_json=True,
    )
    if cls.get("kind") not in ("report", "filing", "policy"):
        return {"ok": False, "file": path.name, "error": f"LLM 返回的 kind 不合法: {cls.get('kind')!r}，文件留在收件台"}
    filename = build_filename(cls, path.stem)
    directory = target_directory(cls)
    directory.mkdir(parents=True, exist_ok=True)
    filename = unique_target(directory, filename)
    dest = directory / filename
    shutil.move(str(path), str(dest))
    return {
        "ok": True,
        "file": path.name,
        "renamed": filename,
        "target": dest.relative_to(config.ROOT).as_posix(),
        "kind": cls["kind"],
        "reason": cls.get("reason", ""),
    }


def classify_inbox():
    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(config.INBOX_DIR.glob("*.pdf"))
    if not pdfs:
        print("收件台（raw/inbox/）没有待归类的 PDF。")
        return []
    results = []
    for p in pdfs:
        print(f"归类中: {p.name} ...")
        try:
            r = classify_one(p)
        except llm.LLMError as e:
            r = {"ok": False, "file": p.name, "error": str(e)}
        if r["ok"]:
            print(f"  -> {r['renamed']}  移入 {r['target']}")
            print(f"     依据: {r['reason']}")
        else:
            print(f"  -> 失败: {r['error']}")
        results.append(r)
    return results
