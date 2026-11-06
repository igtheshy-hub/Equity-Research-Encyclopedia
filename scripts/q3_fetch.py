"""批量取 A 股公司最新三季报（巨潮），先出计划，再按计划下载并归档。

用法：
  python scripts/q3_fetch.py plan      # 只检索，生成 data/_q3_plan.json
  python scripts/q3_fetch.py fetch     # 按计划下载到 raw/filings/<公司>/，已存在同内容跳过
"""
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, fetcher  # noqa: E402
from src.classify import find_duplicate, sanitize, unique_target  # noqa: E402

YEAR = 2026
PLAN = config.DATA_DIR / "_q3_plan.json"

COMPANIES = [
    ("上汽集团", "600104"), ("广汽集团", "601238"), ("比亚迪", "002594"),
    ("长城汽车", "601633"), ("长安汽车", "000625"), ("赛力斯", "601127"),
    ("北汽蓝谷", "600733"), ("千里科技", "601777"), ("宇通客车", "600066"),
    ("金龙汽车", "600686"), ("金杯汽车", "600609"),
    ("三花智控", "002050"), ("上声电子", "688533"), ("中原内配", "002448"),
    ("中国汽研", "601965"), ("中鼎股份", "000887"), ("亚普股份", "603013"),
    ("伯特利", "603596"), ("保隆科技", "603197"), ("光洋股份", "002708"),
    ("华域汽车", "600741"), ("华阳集团", "002906"), ("双环传动", "002472"),
    ("均胜电子", "600699"), ("岱美股份", "603730"), ("德赛西威", "002920"),
    ("拓普集团", "601689"), ("新泉股份", "603179"), ("旭升集团", "603305"),
    ("易实精密", "920221"), ("星宇股份", "601799"), ("星源卓镁", "301398"),
    ("松原安全", "300893"), ("沪光股份", "605333"), ("爱柯迪", "600933"),
    ("瑞鹄模具", "002997"), ("福耀玻璃", "600660"), ("福达股份", "603166"),
    ("科博达", "603786"), ("经纬恒润", "688326"), ("继峰股份", "603997"),
    ("联明股份", "603006"), ("银轮股份", "002126"), ("飞龙股份", "002536"),
    ("骆驼股份", "601311"), ("潍柴动力", "000338"),
    ("春风动力", "603129"), ("隆鑫通用", "603766"), ("爱玛科技", "603529"),
]

TITLE_RE = re.compile(r"(20\d{2})\s*年\s*(?:第三季度|三季度)\s*报告")
BAD = ("摘要", "英文", "English", "更正", "取消", "已取消", "补充")


def _date_of(it):
    try:
        return datetime.fromtimestamp(int(it.get("announcementTime")) / 1000).strftime("%Y%m%d")
    except (TypeError, ValueError):
        return ""


def _pick(items, name):
    out = []
    for it in items:
        title = fetcher._clean_title(it.get("announcementTitle"))
        sec = fetcher._clean_title(it.get("secName"))
        m = TITLE_RE.search(title)
        if not m or int(m.group(1)) != YEAR:
            continue
        if any(b in title for b in BAD):
            continue
        if name not in sec and name not in title:
            continue
        out.append((_date_of(it), title, sec, it))
    out.sort(key=lambda x: (x[0], "全文" in x[1]), reverse=True)
    return out


def _search(name, code):
    hit = fetcher.cninfo_stock(code=code)
    if hit:
        stock = f"{hit.get('code')},{hit.get('orgId')}"
        items = fetcher._cninfo_search("", page_size=50,
                                       category="category_sjdbg_szsh", stock=stock)
        picked = _pick(items, name)
        if picked:
            return picked, f"stock:{stock}"
    items = fetcher._cninfo_search(f"{name} 三季度报告", page_size=50,
                                   category="category_sjdbg_szsh")
    picked = _pick(items, name)
    return picked, "query"


def plan():
    rows = []
    for i, (name, code) in enumerate(COMPANIES, 1):
        try:
            picked, how = _search(name, code)
        except Exception as e:  # noqa: BLE001
            rows.append({"company": name, "code": code, "status": "error", "error": str(e)})
            print(f"[{i}/{len(COMPANIES)}] {name} {code} ERROR {e}")
            time.sleep(1)
            continue
        if not picked:
            rows.append({"company": name, "code": code, "status": "missing"})
            print(f"[{i}/{len(COMPANIES)}] {name} {code} MISSING")
        else:
            date, title, sec, it = picked[0]
            adj = str(it.get("adjunctUrl") or "").strip()
            url = adj if adj.startswith("http") else fetcher.CNINFO_PDF_BASE + adj.lstrip("/")
            rows.append({"company": name, "code": code, "status": "ok",
                         "title": title, "sec": sec, "date": date, "url": url,
                         "how": how, "candidates": len(picked)})
            print(f"[{i}/{len(COMPANIES)}] {name} {code} OK {date} {title} ({len(picked)} cand, {how})")
        time.sleep(1)
    PLAN.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for r in rows if r["status"] == "ok")
    print(f"\n计划写入 {PLAN}：ok={ok} missing={sum(1 for r in rows if r['status']=='missing')} "
          f"error={sum(1 for r in rows if r['status']=='error')}")


def fetch():
    rows = json.loads(PLAN.read_text(encoding="utf-8"))
    results = []
    for i, r in enumerate(rows, 1):
        name = r["company"]
        if r["status"] != "ok":
            results.append(r)
            continue
        stem = sanitize(f"{name}_{YEAR}年三季报_{r['date']}")
        target_dir = config.FILINGS_DIR / sanitize(name)
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            dest = fetcher._download_pdf(r["url"], stem)
        except fetcher.FetchError as e:
            msg = str(e)
            if "内容与已有文件重复" in msg:
                existing = msg.split("：", 1)[-1]
                r2 = dict(r, status="exists", existing=existing)
                print(f"[{i}/{len(rows)}] {name} EXISTS {existing}")
            else:
                r2 = dict(r, status="error", error=msg)
                print(f"[{i}/{len(rows)}] {name} ERROR {msg}")
            results.append(r2)
            time.sleep(1)
            continue
        final = unique_target(target_dir, f"{stem}.pdf")
        dest.replace(target_dir / final)
        r2 = dict(r, status="downloaded", file=(target_dir / final).relative_to(config.ROOT).as_posix(),
                  size=(target_dir / final).stat().st_size)
        results.append(r2)
        print(f"[{i}/{len(rows)}] {name} DOWNLOADED {r2['file']} ({r2['size']} bytes)")
        time.sleep(1)
    out = config.DATA_DIR / "_q3_fetch.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"\n结果写入 {out}：{counts}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    {"plan": plan, "fetch": fetch}[cmd]()
