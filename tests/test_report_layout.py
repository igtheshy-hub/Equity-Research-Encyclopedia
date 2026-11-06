"""报告版式机械检查：表格题注、图题错位、全局编号。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import anchor_match as am, industry_report as ir  # noqa: E402

TABLE = "| 环节 | 代表主体 |\n|---|---|\n| 上游 | 甲公司 |\n"


def test_missing_table_caption_generated():
    md = "## 一、产业链\n\n" + TABLE
    got = ir._fix_tables_figs(md)
    assert "**表1 环节与代表主体**" in got


def test_figure_caption_above_table_relabelled():
    md = "## 一、产业链\n\n**图1 产业链结构**\n\n" + TABLE
    got = ir._fix_tables_figs(md)
    assert "**表1 产业链结构**" in got
    assert "**图1" not in got


def test_orphan_figure_caption_removed():
    md = "## 一、产业链\n\n**图1 产业链结构**\n\n正文。\n"
    got = ir._fix_tables_figs(md)
    assert "**图1" not in got
    assert "正文。" in got


def test_plain_table_caption_kept_and_bolded():
    md = "## 一、产业链\n\n表1 产业链结构\n\n" + TABLE
    got = ir._fix_tables_figs(md)
    assert "**表1 产业链结构**" in got
    assert got.count("表1 产业链结构") == 1


def test_global_renumber_across_sections():
    md = ("# 报告\n\n## 一、A\n\n**表1 甲表**\n\n" + TABLE +
          "\n见表1。\n\n## 二、B\n\n**表1 乙表**\n\n" + TABLE + "\n见表1。\n")
    got = ir._renumber_tables_figs(md)
    assert "**表1 甲表**" in got
    assert "**表2 乙表**" in got
    assert got.count("见表1。") == 1
    assert got.count("见表2。") == 1


def test_month_labels_are_not_numbers():
    assert am.line_terms("2024M10") == []
    assert am.line_terms("2024M10–2026M9") == []


def test_filename_dates_are_not_numbers():
    assert am.line_terms("东吴证券_汽车行业研报_20261102.pdf") == []
    assert am.line_terms("东吴证券_汽车行业研报_20261102") == []
    assert am.line_terms("| 来源 | 东吴证券_汽车行业研报_20261102.pdf |") == []
    assert am.line_terms("销量 73 万辆，见东吴证券_汽车行业研报_20261102.pdf") == [("73", "万辆")]


def test_placeholder_rows_dropped():
    md = ("## 一、产量\n\n**表1 月度增速（2024M10–2026M9）**\n\n"
          "| 月份 | 同比 | 月份 | 同比 |\n|---|---|---|---|\n"
          "| 2024M10 | — | 2025M10 | — |\n"
          "| 2025M3 | 8.4% | 2026M3 | -0.1% |\n")
    got = ir._fix_tables_figs(md)
    assert "2024M10" not in got
    assert "2025M3" in got
    assert "（2025M3–2026M3）" in got


def test_table_with_only_missing_rows_removed():
    md = ("## 一、产量\n\n**表1 月度增速**\n\n"
          "| 月份 | 同比 |\n|---|---|\n"
          "| 2025M3 | — |\n")
    got = ir._fix_tables_figs(md)
    assert "表1" not in got
    assert "|" not in got


def test_fix_is_idempotent():
    md = "## 一、A\n\n" + TABLE
    once = ir._fix_tables_figs(md)
    twice = ir._fix_tables_figs(once)
    assert once == twice


def test_bare_table_numbers_match_unit_source():
    assert am.unit_has_term("库存381万辆", ("381", "")) is False
    assert am.unit_has_term("库存381万辆", ("381", ""), bare_ok=True) is True


def test_bare_revenue_cells_use_row_unit(monkeypatch):
    guoyuan = ("财务数据和估值 2024A 2025A 2026E 2027E 2028E\n"
               "营业收入(百万元) 777102.46 803964.96 904411.77 1005187.40 1139295.39")
    xinan = "利润表（亿元） 2024A 2025E 2026E 2027E 营业收入 7771.02 8662.17 10274.94 12098.26"
    texts = {"g.pdf": guoyuan, "x.pdf": xinan}
    monkeypatch.setattr(ir.wiki_check, "file_paths",
                        lambda: {n: Path(n) for n in texts})
    monkeypatch.setattr(ir, "load_cached", lambda p: {
        "unit": "p",
        "units": [{"n": 1, "text": texts[p.name]} if p.name == "g.pdf" else
                  {"n": 41, "text": texts[p.name]}]})
    md = ("**表1 盈利预测**\n\n"
          "| 指标 | 2024A | 2025A | 2026E | 2027E | 2028E |\n"
          "|---|---|---|---|---|---|\n"
          "| 营业收入（亿元） | 7771.02 [[x.pdf#p41]] | 8039.65 | 9044.12 | "
          "10051.87 | 11392.95 [[g.pdf#p1]] |\n")
    assert ir._table_checks(md) == []
    page = {"body": "- 营业收入7771.02亿元[[x.pdf#p41]]\n"
                    "- 2025年营收8039.65亿元，2026-2028年9044.12/10051.87/11392.95亿元"
                    "[[g.pdf#p1]]"}
    monkeypatch.setattr(ir.wiki, "load_pages", lambda: [page])
    partial = md.replace(" [[g.pdf#p1]] |", " |")
    got = ir._auto_fix_table_anchors(partial)
    assert "[[g.pdf#p1]]" in got
    assert ir._table_checks(got) == []


def test_cross_file_anchor_appended_for_missing_number(monkeypatch):
    page = {"body": "- 续航里程(77%)和充电便利性(71%)仍是购车首要考虑[[b.pdf#p7]]"}
    texts = {"a.pdf": "渗透率50%后增速放缓", "b.pdf": "续航里程(77%)和充电便利性(71%)"}
    monkeypatch.setattr(ir.wiki, "load_pages", lambda: [page])
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {n: Path(n) for n in texts})
    monkeypatch.setattr(ir, "load_cached", lambda p: {
        "unit": "p", "units": [{"n": 1, "text": texts["a.pdf"]}] if p.name == "a.pdf"
        else [{"n": 7, "text": texts["b.pdf"]}]})
    line = "渗透率跨越50%后，续航里程占77%、充电便利性仍是主要考量[[a.pdf#p1]]"
    got, fixes = ir._auto_fix_anchors(line)
    assert "[[b.pdf#p7]]" in got
    assert am.text_overlap(line, texts["b.pdf"]) >= 2


def test_table_anchor_scaled_million_yuan(monkeypatch):
    """报告表格按亿元写、wiki 原文按百万元记，按数值量级也能回挂锚点。"""
    page = {"body": "- 2025年公司营业收入803964.96百万元，2026-2028年预计"
                    "904411.77/1005187.40/1139295.39百万元[[g.pdf#p1]]"}
    texts = {"x.pdf": "营业收入7771.02亿元", "g.pdf": page["body"]}
    monkeypatch.setattr(ir.wiki, "load_pages", lambda: [page])
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {n: Path(n) for n in texts})
    monkeypatch.setattr(ir, "load_cached", lambda p: {
        "unit": "p", "units": [{"n": 41 if p.name == "x.pdf" else 1,
                                "text": texts[p.name]}]})
    md = ("**表1 盈利预测**\n\n"
          "| 指标 | 2024A | 2025A | 2026E | 2027E | 2028E |\n"
          "|---|---|---|---|---|---|\n"
          "| 营业收入 | 7771.02 [[x.pdf#p41]] | 8039.65 | 9044.12 | "
          "10051.87 | 11392.95 |\n")
    assert ir._table_checks(md)
    got = ir._auto_fix_table_anchors(md)
    assert "[[g.pdf#p1]]" in got
    assert ir._table_checks(got) == []


def test_cross_file_anchor_appended_for_multiple_missing(monkeypatch):
    page = {"body": "- 2025年新能源汽车渗透率呈现显著区域分化：欧洲由2024年的17.6%"
                    "提升至23.3%，北美由8.8%小幅回落至8.5%，除中国外的亚洲市场仅约6.8%"
                    "[[b.pdf#p23]]"}
    texts = {"a.pdf": "区域渗透率呈现明显梯度", "b.pdf": page["body"]}
    monkeypatch.setattr(ir.wiki, "load_pages", lambda: [page])
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {n: Path(n) for n in texts})
    monkeypatch.setattr(ir, "load_cached", lambda p: {
        "unit": "p", "units": [{"n": 1 if p.name == "a.pdf" else 23,
                                "text": texts[p.name]}]})
    line = "区域渗透率呈现明显梯度。欧洲23.3%、北美8.5%、除中国外亚洲6.8%[[a.pdf#p1]]"
    got, _fixes = ir._auto_fix_anchors(line)
    assert "[[b.pdf#p23]]" in got


def test_same_file_anchor_appended_for_multiple_missing(monkeypatch):
    wrong = "8月乘用车行业出口同比+78%，其中新能源出口同比+155%"
    right = ("26Q3乘用车出口272万辆创单季新高，其中新能源出口162万辆同比+157%，"
             "占出口比重升至59%")
    monkeypatch.setattr(ir.wiki, "load_pages", lambda: [])
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {"a.pdf": Path("a.pdf")})
    monkeypatch.setattr(ir, "load_cached", lambda _p: {
        "unit": "p", "units": [{"n": 1, "text": wrong}, {"n": 12, "text": right}]})
    line = ("出口是当前唯一高增的需求引擎。26Q3出口272万辆、新能源出口162万辆"
            "同比+157%、占出口59%[[a.pdf#p1]]")
    got, _fixes = ir._auto_fix_anchors(line)
    assert "[[a.pdf#p12]]" in got


def test_same_page_in_pool_twice_still_counts_as_one_source(monkeypatch):
    """同一页在池子里同时以 21 和 p21 出现时，不能算成两个出处把回挂放弃掉。"""
    page = {"body": "- 26Q3乘用车出口272万辆，7/8/9月分别91.8/88.8/91.7万辆[[a.pdf#p21]]"}
    right = "26Q3乘用车出口272万辆，同环比+81%/+12%，7/8/9月分别为91.8/88.8/91.7万辆"
    monkeypatch.setattr(ir.wiki, "load_pages", lambda: [page])
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {"a.pdf": Path("a.pdf")})
    monkeypatch.setattr(ir, "load_cached", lambda _p: {
        "unit": "p", "units": [{"n": 3, "text": "前3季度出口272万辆，同比+81%"},
                               {"n": 21, "text": right}]})
    line = "7至9月分别为91.8/88.8/91.7万辆[[a.pdf#p3]]"
    got, _fixes = ir._auto_fix_anchors(line)
    assert "[[a.pdf#p21]]" in got


def test_table_anchor_disambiguated_by_caption(monkeypatch):
    """18.7% 在库里有多个出处时，用题注里的「零售份额」选对分阵营快照。"""
    pages = [{"body": "- 2023年11月德系品牌零售份额18.7%，日系15.5%，美系7.9%[[a.docx#para6]]"},
             {"body": "- 理想2025年毛利率下滑至18.7%[[b.pdf#p12]]"}]
    texts = {"a.docx": "2023年11月德系品牌零售份额18.7%，日系15.5%，美系7.9%",
             "b.pdf": "理想2025年毛利率下滑至18.7%"}
    monkeypatch.setattr(ir.wiki, "load_pages", lambda: pages)
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {n: Path(n) for n in texts})
    monkeypatch.setattr(ir, "load_cached", lambda p: {
        "unit": "para" if p.name.endswith(".docx") else "p",
        "units": [{"n": 6, "text": texts[p.name]}] if p.name.endswith(".docx")
                 else [{"n": 12, "text": texts[p.name]}]})
    md = ("**表1 分阵营零售份额（2023年11月）**\n\n"
          "| 阵营 | 份额 |\n|---|---|\n| 德系 | 18.7 |\n")
    got = ir._auto_fix_table_anchors(md)
    assert "[[a.docx#para6]]" in got
    assert "[[b.pdf#p12]]" not in got


def test_indicator_ratio_matches_report_percentage(monkeypatch):
    """wiki 指标行记 0.1774，报告写 17.74%，按百分数换算后能核上并回挂到指标行所在页。"""
    page = {"body": "- 【指标】2025FY 毛利率(累计) = 0.1774 [[a.pdf#p126]]"}
    texts = {"a.pdf": "营业收入803,964,958 营业成本661,305,161"}
    monkeypatch.setattr(ir.wiki, "load_pages", lambda: [page])
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {"a.pdf": Path("a.pdf")})
    monkeypatch.setattr(ir, "load_cached", lambda _p: {
        "unit": "p", "units": [{"n": 1, "text": "年度报告摘要"},
                               {"n": 126, "text": texts["a.pdf"]}]})
    assert ir._anchor_checks("2025年毛利率17.74%[[a.pdf#p126]]") == []
    got, _fixes = ir._auto_fix_anchors("2025年毛利率17.74%[[a.pdf#p1]]")
    assert "[[a.pdf#p126]]" in got


def test_multiple_missing_terms_can_use_two_sources(monkeypatch):
    pages = [{"body": "- 【指标】2025FY 毛利率(累计) = 0.1774 [[a.pdf#p126]]"},
             {"body": "- 【指标】2026Q3 毛利率(累计) = 0.1852 [[b.pdf#p8]]"}]
    texts = {"a.pdf": "营业收入803,964,958 营业成本661,305,161", "b.pdf": "三季报利润表"}
    monkeypatch.setattr(ir.wiki, "load_pages", lambda: pages)
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {n: Path(n) for n in texts})
    monkeypatch.setattr(ir, "load_cached", lambda p: {
        "unit": "p", "units": [{"n": 1, "text": "摘要"},
                               {"n": 126 if p.name == "a.pdf" else 8, "text": texts[p.name]}]})
    line = "2025年毛利率17.74%、2026Q3毛利率18.52%[[a.pdf#p1]]"
    got, _fixes = ir._auto_fix_anchors(line)
    assert "[[a.pdf#p126]]" in got
    assert "[[b.pdf#p8]]" in got
    assert ir._anchor_checks(got) == []


def test_decimal_percent_matches_both_directions():
    """小数比率与百分数互认，正反两个方向都成立；整数和无百分号的数字不换。"""
    assert am.unit_has_term("毛利率0.1774", ("17.74", "%"))
    assert am.unit_has_term("毛利率17.74%", ("0.1774", ""))
    assert not am.unit_has_term("占比1", ("100", "%"))
    assert not am.unit_has_term("占比100%", ("1", ""))


def test_per_mille_and_pct_units():
    assert am.line_terms("增长20pct") == [("20", "pct")]
    assert am.unit_has_term("渗透率3‰", ("0.3", "%"))
    assert am.unit_has_term("渗透率0.3%", ("3", "‰"))
    assert not am.unit_has_term("渗透率3‰", ("3", "%"))


def test_single_digit_pe_matches_table_row():
    """估值表 PE 是空格分隔的单数字，一位数在原文里也要能核上「7倍」。"""
    assert am.unit_has_term("吉利汽车 16.92 1836 1.55 2.01 2.39 11 8 7", ("7", "倍"))
    assert not am.unit_has_term("吉利汽车 2027 年 EPS 2.39", ("7", "倍"))


def test_empty_derivation_marker_is_not_exempt(monkeypatch):
    """空的「（测算：）」没有依据，不能据此跳过锚点核验。"""
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {})
    monkeypatch.setattr(ir.wiki, "load_pages", lambda: [])
    line = "份额提升约9.7个百分点（测算：）"
    assert ir._unanchored_nums(line) == ["9.7"]
    assert "仍有空测算标注「（测算：）」" in ir._delivery_problems(line)
    assert ir._unanchored_nums("份额提升约9.7个百分点（测算：65-55.3）") == []
    # 依据写在括号中段也算有依据，不能因为「测算」没有紧跟左括号就整句要求数字出现在原文
    assert ir._unanchored_nums("总市值7772.41亿元（对应总股本91.17亿股，测算：7772.41/91.17）") == []
    line = "总市值7772.41亿元（对应总股本91.17亿股，测算：7772.41/91.17≈85.3元/股）[[a.docx#para6]]"
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {"a.docx": Path("a.docx")})
    monkeypatch.setattr(ir, "load_cached", lambda _p: {
        "unit": "para", "units": [{"n": 6, "text": "总股本91.17亿股"}]})
    assert ir._anchor_checks(line) == []


def test_thousand_yuan_matches_yuan_source():
    """报告按千元写、原文按元列示，是同一数值。"""
    assert am.line_terms("实收资本9,117,198千元") == [("9117198", "千元")]
    assert am.unit_has_term("股本 9,117,198,000.00", ("9117198", "千元"))


def test_thousand_yuan_table_cell_checks(monkeypatch):
    monkeypatch.setattr(ir.wiki, "load_pages", lambda: [])
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {"a.pdf": Path("a.pdf")})
    monkeypatch.setattr(ir, "load_cached", lambda _p: {
        "unit": "p", "units": [{"n": 5, "text": "股本 9,117,198,000.00 实收资本"}]})
    md = ("**表1 主要科目**\n\n| 科目 | 金额 | 来源 |\n|---|---|---|\n"
          "| 实收资本 | 9,117,198千元 | [[a.pdf#p5]] |\n")
    assert ir._table_checks(md) == []


def test_anchor_map_covers_ratio_and_percent_directions():
    pages = [{"body": "- 【指标】2025FY 毛利率(累计) = 0.1774 [[a.pdf#p126]]"},
             {"body": "- 2026Q3 毛利率18.52%[[b.pdf#p8]]"}]
    m = am.number_anchor_map(pages)
    assert ("a.pdf", "p126") in m["17.74"]
    assert ("b.pdf", "p8") in m["0.1852"]


def test_save_report_keeps_heading_on_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(ir.config, "ROOT", tmp_path)
    job = {"title": "测试研报", "report_path": "r.md"}
    sections = [{"heading": "一、甲"}, {"heading": "二、乙"}]
    done = {"一、甲": "第一段。", "二、乙": "## 二、乙\n\n第二段。"}
    ir._save_report(job, sections, done)
    md = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert md.count("## 一、甲") == 1
    assert md.count("## 二、乙") == 1


def test_derived_table_column_marked_cexuan(monkeypatch):
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {})
    md = ("**表1 目标价**\n\n| 项目 | 数值（测算） |\n|---|---|\n"
          "| 目标价区间 | 118–126 元 |\n")
    assert ir._table_checks(md) == []
    plain = ("**表1 目标价**\n\n| 项目 | 数值 |\n|---|---|\n"
             "| 目标价区间 | 118–126 元 |\n")
    assert ir._table_checks(plain)


def test_table_row_anchor_autofixed_from_wiki(monkeypatch):
    page = {"body": "- 库存381万辆，厂商78万辆，渠道303万辆，占比20.5%，可售67天[[a.docx#para6]]"}
    monkeypatch.setattr(ir.wiki, "load_pages", lambda: [page])
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {"a.docx": Path("a.docx")})
    monkeypatch.setattr(ir, "load_cached", lambda _p: {
        "unit": "para",
        "units": [{"n": 6, "text": "库存381万辆，厂商78万辆，渠道303万辆，占比20.5%，可售67天"}]})
    md = ("**表1 库存**\n\n| 时点 | 库存 | 厂商 | 渠道 | 占比 | 天数 |\n"
          "|---|---|---|---|---|---|\n| 2023年12月末 | 381 | 78 | 303 | 20.5% | 67天 |\n")
    got = ir._auto_fix_table_anchors(md)
    assert "[[a.docx#para6]]" in got
    assert ir._table_checks(got) == []


def test_table_row_gets_multiple_anchors_when_no_single_source_covers(monkeypatch):
    """一行里份额与销量分属两页时必须能分别挂锚，不能因为单页覆盖不了整行就放弃。"""
    pages = [{"body": "- 德系品牌零售份额18.7%[[a.docx#para6]]"},
             {"body": "- 2025年新能源乘用车销量205万辆[[b.pdf#p3]]"}]
    texts = {"a.docx": "德系品牌零售份额18.7%", "b.pdf": "2025年新能源乘用车销量205万辆"}
    monkeypatch.setattr(ir.wiki, "load_pages", lambda: pages)
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {n: Path(n) for n in texts})
    monkeypatch.setattr(ir, "load_cached", lambda p: {
        "unit": "para" if p.name.endswith(".docx") else "p",
        "units": [{"n": 6, "text": texts[p.name]}] if p.name.endswith(".docx")
                 else [{"n": 3, "text": texts[p.name]}]})
    md = ("**表2 德系份额与新能源销量**\n\n"
          "| 阵营 | 份额（%） | 销量（万辆） |\n|---|---|---|\n"
          "| 德系 | 18.7 | 205 |\n")
    assert ir._table_checks(md)
    got = ir._auto_fix_table_anchors(md)
    assert "[[a.docx#para6]]" in got and "[[b.pdf#p3]]" in got
    assert ir._table_checks(got) == []


def test_empty_calc_filled_from_same_sentence_numbers():
    """空的「（测算：）」用同句数字补算式；同句凑不出或凑出多解时不硬填。"""
    line = ("叠加10%基础关税后综合27.0%（测算：），相对45.3%的上限具备约18.3pct（测算：）。")
    got = ir._fill_empty_calcs(line)
    assert "27.0%（测算：17.0+10）" not in got  # 17.0 在另一句，不能跨句取数
    assert "综合27.0%（测算：）" in got
    line2 = ("公司17.0%的税率处于分层中的最低档，叠加10%基础关税后综合27.0%（测算：），"
             "相对上汽的45.3%上限具备约18.3pct（测算：）的成本缓冲。")
    got2 = ir._fill_empty_calcs(line2)
    assert "27.0%（测算：17.0+10）" in got2
    assert "18.3pct（测算：45.3-27.0）" in got2
    # 分句后目标数字所在句没有别的数字，NEV 两个口径都在前句，不能猜，保持空白交给审校
    ambiguous = ("BEV份额72.8%、PHEV份额27.2%；BEV份额67.8%、PHEV份额32.2%，"
                 "一年内BEV份额提升约5个百分点（测算：）。")
    assert "（测算：）" in ir._fill_empty_calcs(ambiguous)


def test_anchor_source_window_finds_unit_converted_number(monkeypatch):
    """报告写 73.68亿元、原文写 7,368,306,000.00，证据窗口要能定位到财务费用这一行。"""
    filler = "利润表科目" * 120  # 500 字窗口按页首截取会漏掉后面的财务费用行
    text = filler + " 财务费用 7,368,306,000.00 其中：利息费用 2,810,841,000.00 利息收入 2,330,980,000.00"
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {"a.pdf": Path("a.pdf")})
    monkeypatch.setattr(ir, "load_cached", lambda _p: {
        "unit": "p", "units": [{"n": 8, "text": text}]})
    out = ir._anchor_source_lines([("a.pdf", "p8")], focus=["73.68"])
    assert "财务费用" in out[0]
    assert "利息费用" in out[0]


def test_quarter_scope_mismatch_flags_cumulative_page():
    """句子写单季，锚点页只有累计列头，期间口径不符。"""
    page = "2026年前三季度（1-9月） 其他综合收益的税后净额 -74,381,664.26"
    assert ir._quarter_scope_mismatch("26Q3其他综合收益税后净额-0.74亿元", [page])


def test_quarter_scope_mismatch_allows_cumulative_wording():
    page = "2026年前三季度（1-9月） 其他综合收益的税后净额 -74,381,664.26"
    assert not ir._quarter_scope_mismatch("前三季度其他综合收益税后净额-0.74亿元", [page])


def test_quarter_scope_mismatch_allows_single_quarter_page():
    page = "本报告期 其他综合收益的税后净额 -74,381,664.26"
    assert not ir._quarter_scope_mismatch("26Q3其他综合收益税后净额-0.74亿元", [page])


def test_quarter_scope_mismatch_allows_explicit_quarter_labeling():
    """页内「1-9月/累计」属别的指标时，页面自己把 3Q26 与数字同句标注的不算累计口径。"""
    page = ("国家统计局数据显示，2026年1-9月，中国规模以上企业累计啤酒产量2894.7万千升，"
            "同比下降6.0%。3Q26白酒板块营收及归母净利增速分别为-10.1%、-13.0%，降幅环比收窄"
            "（2Q26白酒板块营收、归母净利增速分别为-17.1%、-21.4%）。")
    line = ("按券商披露口径，白酒板块3Q26营收增速为-10.1%、归母净利增速为-13.0%，"
            "较2Q26（营收-17.1%、归母净利-21.4%）降幅环比收窄")
    assert not ir._quarter_scope_mismatch(line, [page])


def test_quarter_scope_mismatch_keeps_cumulative_labeled_quarter():
    """页面写的是「3Q26累计」，句子照抄也不能当成单季数。"""
    page = ("2026年1-9月全国白酒产量同比下降。3Q26白酒板块累计营收增速-10.1%、"
            "归母净利增速-13.0%。")
    line = "白酒板块3Q26营收增速为-10.1%、归母净利增速为-13.0%"
    assert ir._quarter_scope_mismatch(line, [page])


def test_quarter_scope_mismatch_requires_number_in_quarter_sentence():
    """季度标识与数字不同句时，页面没有把数字标成单季，照样报。"""
    page = "3Q26板块景气分化。前三季度白酒板块营收增速为-10.1%、归母净利增速为-13.0%。"
    line = "白酒板块3Q26营收增速为-10.1%、归母净利增速为-13.0%"
    assert ir._quarter_scope_mismatch(line, [page])


def test_gap_class_terms_only_parses_parentheses_lists():
    assert ir._gap_class_terms("分阵营（自主/合资/豪华/新势力）渗透率") == ["自主", "合资", "豪华", "新势力"]
    assert ir._gap_class_terms("分阵营渗透率") == []
    assert ir._gap_class_terms("销量（万辆）") == []


def test_unanchored_nums_skips_table_refs_and_md_dates():
    """「表16」指向本报告其他表、「10月19-29日」是观察窗口，都不是要挂锚点的数据。"""
    assert ir._unanchored_nums("组合构建上，三条主线的权重切换服从表16的验证节点") == []
    nums = ir._unanchored_nums("10月19-29日飞天茅台批价1685-1700元，普五752-765元，见图3")
    assert set(nums) == {"1685", "1700", "752", "765"}


def test_table_cell_md_date_not_treated_as_data(monkeypatch):
    """单元格里的「10月19-29日」是批价观察窗口，原文写作 10.19-10.29，不算无锚点数字。"""
    page = ("根据百荣酒价及茅粉鲁智深数据，10.19-10.29 飞天茅台批价 1685-1700 元。"
            "10.19-10.29 普五批价 752-765元，国窖1573批价815-820元。")
    monkeypatch.setattr(ir.wiki, "load_pages", lambda: [])
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {"a.pdf": Path("a.pdf")})
    monkeypatch.setattr(ir, "load_cached", lambda _p: {
        "unit": "p", "units": [{"n": 7, "text": page}]})
    md = ("| 环节 | 构成 | 要点 |\n"
          "| --- | --- | --- |\n"
          "| 下游 | 经销商网络与终端消费 | 10月19-29日飞天茅台批价1685-1700元、"
          "普五752-765元、国窖1573批价815-820元[[a.pdf#p7]] |\n")
    assert ir._table_checks(md) == []


def test_anchor_checks_allow_table_ref_sentence():
    md = "三条主线的权重切换服从表16的验证节点。\n"
    assert ir._anchor_checks(md) == []


def test_prediction_placeholder_only_when_cell_is_empty():
    assert ir._placeholder_hits("| 归母净利润 | 预测值 |") == ["预测值"]
    assert ir._placeholder_hits("预测值为本报告测算。") == []


def test_cross_section_issues_reports_conflicting_calls():
    md = ("我们维持买入评级，目标价120.30元，上行空间41.1%。\n"
          "我们给予增持评级，目标价95至115元，上行空间11%。")
    issues = ir._cross_section_issues(md)
    assert any("评级" in x for x in issues)
    assert any("目标价" in x for x in issues)
    assert any("上行空间" in x for x in issues)


def test_cross_section_issues_ignores_single_call():
    md = "我们维持买入评级，目标价120.30元，上行空间41.1%。\n我们采用PE估值。"
    assert ir._cross_section_issues(md) == []


def test_garbled_ratio_flags_replacement_chars():
    from src import fetcher
    assert fetcher._garbled_ratio("����ѡ�ͳ���") > 0.05
    assert fetcher._garbled_ratio("2026年三季报营业收入 12.3 亿元") == 0.0


def test_decode_prefers_declared_charset():
    from src import fetcher

    class Resp:
        def __init__(self, content, ctype=""):
            self.content = content
            self.headers = {"Content-Type": ctype} if ctype else {}

    text = "中汽协 2026年9月客车销量 4.8 万辆"
    assert fetcher._decode(Resp(text.encode("gb18030"), "text/html; charset=gb18030")) == text
    assert fetcher._decode(Resp(text.encode("utf-8"))) == text


def test_rejected_repair_cannot_delete_sourced_number(monkeypatch):
    """原文能核上的数字，修复轮不得以「未披露」删掉。"""
    old = "2025年毛利率8%，同比提升1个百分点 [[a.pdf#p6]]"
    new = "2025年毛利率未披露"
    monkeypatch.setattr(ir, "_line_anchors_cover", lambda line: True)
    reason = ir._repair_rejected(old, new, "正文。\n" + old + "\n")
    assert "不允许改成未披露" in reason
    monkeypatch.setattr(ir, "_line_anchors_cover", lambda line: False)
    assert ir._repair_rejected(old, new, "正文。\n" + old + "\n") == ""


def test_decode_ignores_default_western_charset():
    """服务器声明 iso-8859-1、实际是 gb18030 时，不能照声明解成乱码。"""
    from src import fetcher

    class Resp:
        def __init__(self, content, ctype):
            self.content = content
            self.headers = {"Content-Type": ctype}

    text = "中汽协 2026年9月客车销量 4.8 万辆"
    raw = text.encode("gb18030")
    assert fetcher._decode(Resp(raw, "text/html; charset=ISO-8859-1")) == text


def test_table_quarter_header_flags_cumulative_source(monkeypatch):
    """表头写 26Q3，来源页只有累计列头时，整表期间口径不符。"""
    page = "2026年前三季度（1-9月） 营业收入 12.3"
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {"a.pdf": Path("a.pdf")})
    monkeypatch.setattr(ir, "load_cached", lambda p: {
        "unit": "p", "units": [{"n": 7, "text": page}]})
    md = ("**表1 单季经营**\n\n"
          "| 指标 | 26Q3 |\n|---|---|\n"
          "| 营业收入（亿元） | 12.3 [[a.pdf#p7]] |\n")
    probs = ir._table_checks(md)
    assert any("期间口径" in x for x in probs)
    ok = md.replace("26Q3", "前三季度").replace("单季经营", "前三季度经营")
    assert not [x for x in ir._table_checks(ok) if "期间口径" in x]
