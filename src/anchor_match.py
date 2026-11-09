"""数字与锚点的匹配：摄入、成文、审校共用同一套口径。

只做机械比对，不判断语义。中文数字先归一，再按量级容差比；报告里的
「亿元」与原文的「千元」允许换算后相等。
"""
import re
import unicodedata

from . import wiki

_TEXT_CORE_RE = re.compile(r"[^一-鿿A-Za-z]+")


def _text_core(text):
    text = unicodedata.normalize("NFKC", str(text or ""))  # PDF 常抽出「⾄」「⾼」等康熙部首变体
    return _TEXT_CORE_RE.sub("", wiki.ANCHOR_RE.sub("", text))


def text_overlap(a, b, size=4):
    """非数字文字的 4 字片段重合数。只靠数字对上的页面不算同一语义，防止把补贴句挂到车价页。"""
    ca, cb = _text_core(a), _text_core(b)
    if len(ca) < size or len(cb) < size:
        return 0
    grams = {ca[i:i + size] for i in range(len(ca) - size + 1)}
    return sum(1 for g in grams if g in cb)

NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# 文件名里的日期不是数据：「东吴证券_汽车行业研报_20261102.pdf」这类来源列不能当数字核验
_FILE_TOKEN_RE = re.compile(
    r"[^\s，,。；;、：:（）()【】\[\]{}<>《》]*\.(?:pdf|docx?|xlsx?|pptx?|csv|txt|md)\b", re.I)
_FILE_DATE_RE = re.compile(r"[^\s，,。；;、：:（）()【】\[\]{}<>《》]*_\d{8}(?!\d)")
UNIT_RE = (r"(?:个百分点|百分点|万辆|万台|万套|万元|亿美元|百万元|千元|万人|万家|万千瓦时|万千瓦|"
           r"亿美元|亿元|亿|万|元|辆|台|套|家|人|个|倍|吨|GW|GWh|MW|MWh|公里|km|度|瓦|"
           r"pct|ppt|%|％|‰)")
# 百分号类单位。比率（0.1774）与百分数（17.74%）只在这类单位与无单位数值之间互认。
PCT_UNITS = ("%", "％", "pct")
TERM_RE = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)\s*(余)?\s*(" + UNIT_RE + r")?")
_NUM_SCALES = (1, 1e3, 1e4, 1e5, 1e8, 1e-3, 1e-4, 1e-5, 1e-8)
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_BIG = {"万": 10000, "亿": 100000000}
_CN_RUN_RE = re.compile(r"[零〇一二三四五六七八九十百千万亿两]+")


def _cn_run_to_arabic(run):
    if not run:
        return run
    if not any(ch in _CN_UNITS or ch in _CN_BIG for ch in run):
        return "".join(str(_CN_DIGITS[ch]) for ch in run)
    total = section = num = 0
    for ch in run:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            section += (num or 1) * _CN_UNITS[ch]
            num = 0
        elif ch in _CN_BIG:
            section = (section + num) * _CN_BIG[ch]
            total += section
            section = num = 0
    return str(total + section + num)


def normalize_cn_numerals(text):
    """年报常用「二零二五年四月十日」，比对前归一成阿拉伯数字；单字只在年月日前后才转。"""
    def repl(m):
        run = m.group(0)
        if len(run) == 1 and m.string[m.end():m.end() + 1] not in "年月日季度":
            return run
        return _cn_run_to_arabic(run)
    return _CN_RUN_RE.sub(repl, text)


def num_variants(n):
    """0175 与 00175 这类前导零差异，按去掉前导零的写法再试一次。"""
    out = [n]
    stripped = n.lstrip("0")
    if stripped and stripped != n:
        out.append(stripped)
    return out


def _number_hit(text, n):
    """按完整数字边界匹配：10 不能命中 2026.10 里的月份，4.8 不能命中 14.8；
    允许前导零，00175 与 0175 视为同一个数。"""
    clean = str(text or "").replace(",", "")
    if re.fullmatch(r"(?:19|20)\d{2}", n):
        return re.search(r"(?<!\d)" + re.escape(n) + r"(?!\d)", clean) is not None
    pattern = r"(?<![\d.])0*" + re.escape(n) + r"(?![\d.])"
    return re.search(pattern, clean) is not None


def has_number(text, text_norm, n):
    return any(_number_hit(text, v) or _number_hit(text_norm, v) for v in num_variants(n))


def page_numbers(text):
    out = []
    for raw in NUM_RE.findall(text):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def num_on_page(value, nums, same_scale=True):
    """报告按亿元写、原文按千元列示时，换算量级后再比，相对差 2‰ 内算命中。

    same_scale=False 时只认换算过量级的命中：同量级的近似值（22.9 与 22.94）不算，
    供救火提示挑页用，避免把模型引到只有形似数字的页。
    """
    for scale in _NUM_SCALES:
        if scale == 1 and not same_scale:
            continue
        target = value * scale
        for p in nums:
            if p and abs(p - target) <= abs(p) * 0.002:
                return True
    return False


def unit_has_number(text, n):
    text_norm = normalize_cn_numerals(text or "")
    if has_number(text or "", text_norm, n):
        return True
    try:
        return num_on_page(float(n), page_numbers(text_norm))
    except ValueError:
        return False


def numbers_in_text(text, min_digits=2, skip_years=False):
    """抽出可核验的数字。年份与一位数不参与，免得满页都是 1、2、2025。"""
    out = []
    for raw in NUM_RE.findall(text or ""):
        n = raw.replace(",", "")
        if len(n.replace(".", "")) < min_digits:
            continue
        if skip_years and re.fullmatch(r"(?:19|20)\d{2}", n):
            continue
        if n not in out:
            out.append(n)
    return out


def line_numbers(line, anchors_removed=True):
    text = wiki.ANCHOR_RE.sub("", line) if anchors_removed else line
    return numbers_in_text(text)


def number_terms(text, skip_years=True):
    """抽出「数字 + 紧跟单位」的配对。没有单位的短数字（如 10）不能和 10 万、10% 混同。"""
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = _FILE_TOKEN_RE.sub(" ", text)
    text = _FILE_DATE_RE.sub(" ", text)
    # 日期与月份区间不是数据：「2026年1–5月」里的 1、5 不能当销量数字去核锚点
    text = re.sub(r"(?:19|20)\d{2}\s*年\s*\d{1,2}\s*[-–—~至]\s*\d{1,2}\s*月", " ", text)
    text = re.sub(r"(?<![\d.])\d{1,2}\s*[-–—~至]\s*\d{1,2}\s*月", " ", text)
    text = re.sub(r"(?:19|20)\d{2}\s*[-年/]\s*\d{1,2}(?:\s*[-月/]\s*\d{1,2}\s*日?)?", " ", text)
    # 26Q3、2026Q3 这类季度标签里的 26 不是统计数字
    text = re.sub(r"(?<![\d.])(?:19|20)?\d{2}\s*[Qq]\s*[1-4](?!\d)", " ", text)
    out = []
    for m in TERM_RE.finditer(text):
        n = m.group(1).replace(",", "")
        unit = m.group(3) or ""
        if skip_years and not unit and re.fullmatch(r"(?:19|20)\d{2}", n):
            continue
        # 匹配尾部带 \s*，切片前先跳过空白；空串 in 任何字符串都为真，必须先判非空
        rest = text[m.end():].lstrip()
        if not unit and rest[:1] and rest[0] in "年月日季度号":
            continue
        if unit == "个" and rest.startswith("月"):
            continue  # 前9个月是时段，不是统计数字
        if unit and rest.startswith(("以上", "以下", "及以上", "及以下")):
            continue  # 30万元以上是价格带定义，不是测量值
        if n == "0":
            out.append((n, unit, m.start(), m.end()))
            continue
        before_ch = text[:m.start()].rstrip()[-1:]
        if not unit and before_ch in ("M", "m") and len(n) <= 2:
            continue  # 2024M10、2025M3 是月份标签，不是统计数字
        if text[:m.start()].rstrip().endswith("第"):
            continue  # 第1梯队、第6名的序号不是统计数字
        if len(n.replace(".", "")) < 2 and not unit:
            # 区间端点（2万–5）单独保留，其余单数字当噪声
            before = text[:m.start()].rstrip()[-1:]
            after = rest[:1]
            if (not before or before not in "-–—~至") and (not after or after not in "-–—~至"):
                continue
        out.append((n, unit, m.start(), m.end()))
    # 「904411.77/1005187.40/1139295.39百万元」「91.8、88.8、91.7万辆」这类列举，
    # 尾部单位适用于整组。分隔符限斜杠与顿号逗号，中间夹别的字就不算（避免把无关数字串起来）
    for k in range(len(out) - 1, 0, -1):
        n, unit, start, _end = out[k]
        prev_n, prev_unit, prev_start, prev_end = out[k - 1]
        if not unit or prev_unit:
            continue
        if re.fullmatch(r"\s*[/、，,]\s*", text[prev_end:start]):
            out[k - 1] = (prev_n, unit, prev_start, prev_end)
    return [(n, u) for n, u, _s, _e in out]


def line_terms(line, skip_years=True):
    return number_terms(wiki.ANCHOR_RE.sub("", line), skip_years=skip_years)


def term_value(n, unit):
    """换算到同一量级：10 万 = 100000，1.28 亿 = 128000000，% 与百分点按数值本身比。"""
    try:
        value = float(str(n).replace(",", ""))
    except ValueError:
        return None
    if "亿" in unit:
        return value * 1e8
    if "百万" in unit:
        return value * 1e6
    if "千元" in unit:
        return value * 1e3
    if "万" in unit:
        return value * 1e4
    if "‰" in unit:
        return value / 10
    return value


def _pct_ratio_match(n, unit, pn, pu, target, pv):
    """小数比率与百分数互认：0.1774 ↔ 17.74%，正反两个方向都换。

    只在恰好一侧带百分号时换，且小数一侧必须带小数点，免得把整数百分比
    压成比率、或把「18 家」放大成 1800%。
    """
    rep_pct = unit in PCT_UNITS
    src_pct = pu in PCT_UNITS
    if rep_pct == src_pct:
        return False
    if rep_pct:
        return "." in pn and abs(pv * 100 - target) <= max(abs(pv * 100), abs(target)) * 0.002
    return "." in n and abs(target * 100 - pv) <= max(abs(target * 100), abs(pv)) * 0.002


def _round_tol(n, target):
    """报告数字按自身显示精度四舍五入的误差上限。

    原文 0.38352013 报告写 0.38%、原文 73,xxx,xxx 元报告写 0.74 亿元，
    都是合法的四舍五入；写几位小数就允许原值落在该精度半个单位内（按单位倍数放大）。
    """
    try:
        raw = float(str(n).replace(",", ""))
    except ValueError:
        return 0.0
    if raw == 0:
        return 0.0
    s = str(n).replace(",", "")
    step = 0.5 * 10 ** (-len(s.split(".")[1])) if "." in s else 0.5
    return abs(target) / abs(raw) * step


def unit_has_term(text, term, bare_ok=False, round_ok=False, scales=None):
    """源单元里要有同量级的数字。带单位的数字按换算值比，避免 10 万被 10% 或 20 家命中。

    表格抽取常把数字和单位分到不同单元格，源里只有裸数字时，再按原始数值比一次；
    bare_ok=True 时反向也放行（报告单元格只写裸数字、单位在表头，源里是「78 万辆」）。
    round_ok=True 时（报告侧核验）容忍写作四舍五入：报告按两位小数写的数，原值落在
    该精度半个单位内算命中，摄入侧保持精确比对不变。
    scales 限定裸数字允许的换算倍数：页面已声明「千元/万元」时只认该级，避免千元按万元
    顶替通过（待修清单 78）。
    """
    n, unit = term
    target = term_value(n, unit)
    if target is None:
        return False
    if target == 0 and re.search(r"(?<![\d.])[-—‐](?![\d.])", text or ""):
        return True  # 财报空值列用「-」，知识行按 0 记录
    try:
        raw = float(str(n).replace(",", ""))
    except ValueError:
        raw = None
    for pn, pu in number_terms(text):
        pv = term_value(pn, pu)
        if pv is None:
            continue
        if abs(pv - target) <= max(abs(pv), abs(target)) * 0.002:
            return True
        if round_ok and abs(pv - target) <= _round_tol(n, target):
            return True
        # 指标行常把比率记成 0.1774，报告写成 17.74%，两个方向都按百分数换算后再比
        if _pct_ratio_match(n, unit, pn, pu, target, pv):
            return True
        # 财报正文常以千元列示、知识行换算成元，源里只有裸数字时允许千/万元级差
        if not pu and abs(target) >= 1000:
            for scale in (scales or (1e3, 1e4, 1e6, 1e8)):
                scaled = pv * scale
                if abs(scaled - target) <= max(abs(scaled), abs(target)) * 0.002:
                    return True
        if unit and not pu and raw is not None:
            try:
                if abs(float(pn) - raw) <= max(abs(float(pn)), abs(raw)) * 0.002:
                    return True
            except ValueError:
                continue
        if bare_ok and not unit and raw is not None:
            # 报告单元格只写裸数字、单位在表头，源里带「亿元/百万元」：换算量级后再比
            for scale in ((1, 1e4, 1e6, 1e8) if pu else (1,)):
                scaled = raw * scale
                if abs(pv - scaled) <= max(abs(pv), abs(scaled)) * 0.002:
                    return True
    # 估值表的 PE 是空格分隔的单数字（「11 8 7」），number_terms 会把无单位的一位数当噪声；
    # 只在「倍」上直接按数字边界找一次，避免 7 倍核不上表里的 7
    if unit == "倍" and raw is not None:
        if re.search(r"(?<![\d.])" + re.escape(n) + r"(?![\d.])", text or ""):
            return True
    return False


def unit_covers_terms(unit_text, terms, bare_ok=False, round_ok=False):
    return all(unit_has_term(unit_text or "", t, bare_ok=bare_ok, round_ok=round_ok)
               for t in terms)


def unit_covers_numbers(unit_text, nums):
    return all(unit_has_number(unit_text or "", n) for n in nums)


def best_units_for_terms(units, terms, hint_text="", min_overlap=2):
    """给一组数字找能覆盖它们的单元集合；找不到返回空列表。

    先试单个单元；单页覆盖不了再按数字分组，允许一句话跨页挂多个锚点。
    给了 hint_text 时，候选单元还必须和原句有 4 字片段重合，避免只凭数字挂到语义无关的页。
    min_overlap 默认 2；报告里短句改写多，可降到 1。
    """
    if not terms:
        return []
    texts = [(u.get("n"), u.get("text") or "") for u in units]
    singles = []
    for n, text in texts:
        if unit_covers_terms(text, terms):
            overlap = text_overlap(hint_text, text) if hint_text else 0
            if not hint_text or overlap >= min_overlap:
                singles.append((overlap, n))
    if singles:
        return [max(singles)[1]]  # 同页全覆盖时取语义最接近的一页，不按页序先到先得
    remaining = set(terms)
    picked = []
    while remaining:
        best = None
        for n, text in texts:
            hit = {t for t in remaining if unit_has_term(text, t)}
            if not hit:
                continue
            overlap = text_overlap(hint_text, text) if hint_text else 0
            if hint_text and overlap < min_overlap:
                continue
            score = (len(hit), overlap)
            if best is None or score > best[0]:
                best = (score, n, hit)
        if best is None:
            return []
        picked.append(best[1])
        remaining -= best[2]
    return picked


def best_units_for_numbers(units, nums):
    return best_units_for_terms(units, [(n, "") for n in nums])


def number_anchor_map(pages):
    """从 wiki 页面建立「数字 → 该数字在库内的锚点集合」，成文与审校共用。

    只收一行里同时带数字和锚点的知识行；一行多锚点时，数字归到该行全部锚点，
    后续按页核验时再收敛到真正含这个数字的页。
    """
    out = {}
    for p in pages:
        for ln in str(p.get("body") or "").splitlines():
            anchors = wiki.ANCHOR_RE.findall(ln)
            if not anchors:
                continue
            pct_line = any(u in ln for u in PCT_UNITS)
            pm_line = "‰" in ln
            for n in line_numbers(ln):
                out.setdefault(n, set()).update(anchors)
                try:
                    v = float(n)
                except ValueError:
                    continue
                if "." in n and 0 < v < 10:
                    # 小数比率 ↔ 百分数：0.1774 的锚点也给 17.74 挂一份候选
                    out.setdefault(f"{v * 100:g}", set()).update(anchors)
                if pct_line and "." in n and v > 0:
                    # 原文写 17.74% 时，报告按比率 0.1774 写也归到同一出处
                    out.setdefault(f"{v / 100:g}", set()).update(anchors)
                if pm_line and v > 0:
                    # 原文按千分号写 3‰，报告写 0.3% 时数字是 0.3
                    out.setdefault(f"{v / 10:g}", set()).update(anchors)
    return out


def anchors_for_numbers(nums, mapping):
    """返回能支撑这些数字的锚点集合；数字在库里没有记录时返回空集。"""
    found = set()
    for n in nums:
        found.update(mapping.get(n) or set())
    return found
