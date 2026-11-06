# 取数来源表（程序运行时读取）

本文件是 data-sources 技能的机器可读来源表。网页端研报管线的缺口提示词（src/industry_report.py 的 GAPS_PROMPT）从这里读结构化源枚举；网络检索抓取成功的新网址由 src/fetcher.py 自动追加到下方网页来源块。两个标记之间的内容一行一个 JSON 对象，程序按行解析，改动时保持一行一条，不要加注释或换行折行。

维护规则：结构化源增删改同时改本文件与 SKILL.md 的源清单；网页来源由程序自动追加，也可手动补 `{"title": "...", "url": "...", "added": "YYYY-MM-DD"}` 一行。

## 结构化源（run_fetch 源 id）

<!-- SOURCES:STRUCTURED:START -->
{"id": "stats_series", "name": "国家统计局", "url": "https://www.stats.gov.cn", "login": false, "doc": "params {{\"keyword\": \"中文指标名，多个用分号\", \"region\": \"全国或省市\", \"freq\": \"auto / 月 / 季 / 年，只填一个\", \"periods\": 期数}}"}
{"id": "akshare_series", "name": "AKShare 宏观序列", "url": "", "login": false, "doc": "params {{\"series\": \"以下之一：{SERIES}\", \"periods\": 期数}}"}
{"id": "eastmoney_reports", "name": "东财研报下载", "url": "https://data.eastmoney.com/report/", "login": false, "doc": "params {{\"qtype\": \"只填数字：0=个股，1=行业，2=策略\", \"industry\": \"行业\", \"stock_code\": \"代码\", \"broker\": \"券商可空\", \"limit\": 份数}}"}
{"id": "cninfo_announce", "name": "巨潮 A 股公告（港股不覆盖）", "url": "http://www.cninfo.com.cn", "login": false, "doc": "params {{\"query\": \"检索词\", \"code\": \"A股代码可空\", \"category\": \"分类代码可空\", \"days\": 天数, \"limit\": 份数}}"}
{"id": "hkex_announce", "name": "港交所公告", "url": "https://www.hkexnews.hk", "login": false, "doc": "params {{\"company\": \"公司名\", \"code\": \"港股代码\", \"category\": \"annual / interim / quarterly / esg / results / notice / all，只填一个\", \"limit\": 份数}}；取定期报告只填 annual / interim / quarterly 对应类别，禁止用 all（会抓回公司章程、董事名单等无关公告）"}
{"id": "annual_report", "name": "公司年报", "url": "", "login": false, "doc": "params {{\"company\": \"公司名\", \"market\": \"a 或 hk\"}}"}
{"id": "periodic_report", "name": "公司定期报告", "url": "", "login": false, "doc": "params {{\"company\": \"公司名\", \"code\": \"代码可空\", \"market\": \"a 或 hk\", \"kind\": \"interim / q1 / q3 / quarterly，只填一个\", \"days\": 天数}}；每次只取最近一期，要对比多期时缺口仍只列最近一期，更早期间由撰稿人从库内已有材料取"}
{"id": "sample_chain", "name": "行业样板研报下载链", "url": "", "login": false, "doc": "params {{\"industry\": \"行业\", \"limit\": 份数}}"}
{"id": "cpca_sales", "name": "乘联会汽车销量", "url": "https://www.cpcaauto.com", "login": false, "doc": "params {{\"charttype\": \"只填数字：1=整体，2=厂商，3=车型，4=国别，5=级别，6=新能源\", \"metric\": \"产量 / 批发 / 零售 / 出口 / 当月销量 / 累计销量 / 当月份额 / 累计份额 / 全部，只填一个\", \"months\": 月数}}"}
{"id": "csindex_index", "name": "中证指数行情估值与权重", "url": "https://www.csindex.com.cn", "login": false, "doc": "params {{\"code\": \"指数代码\"}}"}
{"id": "pbc_stat", "name": "人民银行统计表", "url": "http://www.pbc.gov.cn", "login": false, "doc": "params {{\"dataset\": \"货币统计概览 / 金融业机构资产负债统计 / 金融机构信贷收支统计 / 金融市场统计，只填一个\", \"keyword\": \"表名关键词\", \"months\": 月数}}"}
{"id": "fred_series", "name": "FRED 宏观序列（含汇率）", "url": "", "login": false, "doc": "params {{\"series_id\": \"序列号，如 DEXCHUS=美元兑人民币\", \"periods\": 期数}}"}
{"id": "imf_series", "name": "IMF DataMapper", "url": "", "login": false, "doc": "params {{\"indicator\": \"指标代码\", \"countries\": \"CHN,USA\", \"periods\": 期数}}"}
{"id": "dbnomics_series", "name": "DBnomics", "url": "", "login": false, "doc": "params {{\"series\": \"provider/dataset/series\", \"periods\": 期数}}"}
{"id": "worldbank_series", "name": "World Bank", "url": "", "login": false, "doc": "params {{\"indicator\": \"指标代码\", \"countries\": \"CHN\", \"periods\": 期数}}"}
{"id": "caam_article", "name": "中汽协行业要闻文字稿", "url": "http://www.caam.org.cn", "login": false, "doc": "params {{\"keyword\": \"标题关键词\", \"category\": \"行业要闻 / 协会动态，只填一个\", \"limit\": 篇数}}；产销数据文章正文是图片，不要用它取销量，改用 cpca_sales"}
{"id": "web_search", "name": "网络检索兜底", "url": "", "login": false, "doc": "params {{\"query\": \"检索式\", \"limit\": 份数}}"}
<!-- SOURCES:STRUCTURED:END -->

## 网页来源（网络检索成功过的外部网址）

<!-- SOURCES:WEB:START -->
{"title": "中华人民共和国2025年国民经济和社会发展统计公报", "url": "https://www.stats.gov.cn/sj/zxfbhjd/202602/t20260228_1962662.html", "added": "2026-10-29"}
{"title": "2025年日历全年完整图_带农历节假日放假安排_高清可打印 ...", "url": "https://rili.zzhuangli.com/year/2025", "added": "2026-10-29"}
{"title": "中华人民共和国2025年国民经济和社会发展统计公报", "url": "https://www.gov.cn/lianbo/202602/content_7059980.htm", "added": "2026-10-29"}
{"title": "万年历2025年日历表_日历网", "url": "https://www.rili.com.cn/wannianli/2025/", "added": "2026-10-29"}
{"title": "2025年全国两会_中国政府网", "url": "https://www.gov.cn/zhuanti/2025qglh/", "added": "2026-10-29"}
{"title": "2025年政府工作报告 - 中国新闻网", "url": "https://www.chinanews.com.cn/gn/z/lhzfgzbg2025/index.shtml", "added": "2026-10-29"}
{"title": "2025全国两会大型融媒体专题_新华网", "url": "https://www.news.cn/politics/2025lh", "added": "2026-10-29"}
{"title": "新浪网", "url": "https://www.sina.com.cn/", "added": "2026-10-29"}
{"title": "新浪首页", "url": "https://www.sina.com.cn/temp/", "added": "2026-10-29"}
{"title": "新闻中心首页_新浪网", "url": "https://news.sina.com.cn/", "added": "2026-10-29"}
{"title": "新是什么意思_新的翻译_音标_读音_用法_例句_爱词霸在线词典", "url": "https://www.iciba.com/word?w=%E6%96%B0", "added": "2026-10-29"}
{"title": "新字笔画、笔顺、笔划 - 新字怎么写?", "url": "https://www.hanzipi.com/%E6%96%B0.html", "added": "2026-10-29"}
{"title": "WinRAR - 压缩软件 老牌压缩软件知名产品 经典装机软件之一", "url": "https://www.winrar.com.cn/", "added": "2026-10-29"}
{"title": "Bandizip 官方网站 - 免费压缩软件下载 (Windows)", "url": "https://cn.bandisoft.com/bandizip/", "added": "2026-10-29"}
{"title": "7-Zip 官方中文网站 - Sparanoid", "url": "https://sparanoid.com/lab/7z/", "added": "2026-10-29"}
{"title": "抖店官网 - 抖音小店，抖音电商官方商家入驻平台登录入口", "url": "https://fxg.jinritemai.com/", "added": "2026-10-29"}
{"title": "抖音小时达官网-小时达入驻-小时达商家入驻", "url": "https://fxg.jinritemai.com/instant-retail", "added": "2026-10-29"}
{"title": "抖店入驻材料费用与入驻流程-类目抖音小店保证金-抖店入驻 ...", "url": "https://fxg.jinritemai.com/enter/quali-fee", "added": "2026-10-29"}
{"title": "可比增长和同比增长什么区别？_百度知道", "url": "https://zhidao.baidu.com/question/469970379.html", "added": "2026-10-29"}
{"title": "2月 新能源 车零售 渗透率 21.8%： 自主 品牌持续领跑, 合资 与 豪华 品牌...", "url": "http://www.21jingji.com/article/20220308/herald/247a5475d5bc975bbb469f52dc2c987a.html", "added": "2026-10-29"}
{"title": "政策底的宏观含义", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793774621&ver=7067&signature=o4Wq6OgjVQRtdvlVktJf2uYXeivyVVO8nNfWN1TvAZ-1EVopVHuwhkmEviZiBmXohRnFlMtBN49Nvc991-iG3x9cwJ5eBp5C4fY4qtxSZ-3SQ2qnqkm3Vq2ATF3xjnvF&new=1", "added": "2026-11-04"}
{"title": "2025 .11.17 中金大宗:有色 2026 展望, 铜 、 铝 、 碳酸锂2026 供需分析...", "url": "https://www.bilibili.com/video/BV1QQCeBoEUc/", "added": "2026-11-04"}
{"title": "沪 铝 震荡运行 碳酸锂 仍以逢高沽空为主| 碳酸锂 _新浪财经_新浪网", "url": "https://finance.sina.com.cn/money/future/rese/2024-06-03/doc-inaxmwyc4615205.shtml", "added": "2026-11-04"}
{"title": "今日 碳酸锂 价格 行情 （2024年9月6日）_市场 报价 _期市动态_91金属网", "url": "https://news.91jinshu.com/shichangbaojia/202409/70735.html", "added": "2026-11-04"}
{"title": "国泰君安： 2025 年 锂价 7-9 万元波动_预期_需求_供给", "url": "https://www.sohu.com/a/836319773_639898", "added": "2026-11-04"}
{"title": "铜 价短期易涨难跌 铝 价有望继续偏强|沪 铝 |云母| 碳酸锂 |氧化 铝 _网易订阅", "url": "https://www.163.com/dy/article/JTUCKDI805568W0A.html", "added": "2026-11-04"}
{"title": "有色行业周报: 铜铝价格上涨 碳酸锂价格 强势|铜铝_新浪财经_新浪网", "url": "https://finance.sina.com.cn/stock/relnews/cn/2025-01-21/doc-ineftwev7857059.shtml", "added": "2026-11-04"}
{"title": "专家解读|2022 年 10 月 全国 乘用车 市场 库存 分析", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793774852&ver=7067&signature=dytLxWOFiSkb0*oV1V7wnI5vVVnv8G820hR2eI8-b170XQHCPA4rTtOymQ3s-*SJGc3KzsKE88qNbZPfuJXA9yWH551qSDCOK24Vej2qpSB9gwUdJZ5mYwaxd2eGMza6&new=1", "added": "2026-11-04"}
{"title": "乘联会 ：全国 乘用车 市场去年12月末 库存 381万辆、 库存 67天|界面新闻...", "url": "https://www.jiemian.com/article/10756197.html", "added": "2026-11-04"}
{"title": "2025年 1 月乘用车库存 降至299万台,市场在转折中迎风而行_搜狐汽车...", "url": "https://www.sohu.com/a/861962513_122066676", "added": "2026-11-04"}
{"title": "乘联会2025 -今日头条", "url": "https://www.toutiao.com/topic/7481429180241053731/", "added": "2026-11-04"}
{"title": "乘联会 ：全国 乘用车 市场总体 库存 将进入去 库存 周期_凤凰网", "url": "https://finance.ifeng.com/c/8VLqoHanAnd", "added": "2026-11-04"}
{"title": "乘联会 ：1月1日-28日新能源车市场零售59.6万辆 同比 去年同期增长92%", "url": "https://news.cnstock.com/news,bwkx-202401-5185396.htm", "added": "2026-11-04"}
{"title": "乘联会 :全国 乘用车 市场4 月 末 库存 323万辆 库存 状态大幅改善_汽车_...", "url": "http://auto.china.com.cn/news/20230522/723124.shtml", "added": "2026-11-04"}
{"title": "乘联会 ：4月1-6日 乘用车 市场零售20.6万辆 同比 下降8%", "url": "https://www.zhitongcaijing.com/content/detail/1101091.html", "added": "2026-11-04"}
{"title": "乘联会 ：1月 乘用车 市场零售 同比 增长57.4%_汽车频道_中国青年网", "url": "https://auto.youth.cn/xw/202402/t20240209_15073163.htm", "added": "2026-11-04"}
{"title": "2023年 乘用车 零售累销2169.9万辆 同比 增5.6%| 乘联会 _网易汽车", "url": "https://www.163.com/auto/article/IO1D1V1N000884MS.html", "added": "2026-11-04"}
{"title": "2025年3 月轿车 / SUV / MPV销量 排行榜_易车", "url": "https://hao.yiche.com/wenzhang/99684581/", "added": "2026-11-04"}
{"title": "2024年10 月轿车 / SUV / MPV销量 (完整版)|mpv| suv |乘用车| 零售量 |豪...", "url": "https://www.163.com/dy/article/JH1DTH6205566SBU.html", "added": "2026-11-04"}
{"title": "数读1 月 销量榜: 轿车、SUV、MPV 三大市场有何变化?|suv|mpv|五菱...", "url": "https://www.163.com/dy/article/HT5TEMED052784D6.html", "added": "2026-11-04"}
{"title": "【中泰汽车】海安集团 26Q3 点评:Q3收入创新高、 盈利 修复稳...", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793775139&ver=7067&signature=f06KSzG9q05EWRhGPKGo79fBOnv5rwZKLU-HuucPBlon3Eh4m3ggfuh68qm-nySbGSn24eAFsRe3rHGF0yUR9XxI3wBG9bXWEqSULIs1S0kHQJGAJlfG3TDqLmArwGH8&new=1", "added": "2026-11-04"}
{"title": "乘用车 整车 单车利润 专题报告-市场解读.docx - 人人文库", "url": "https://www.renrendoc.com/paper/327214199.html", "added": "2026-11-04"}
{"title": "中国 车企 的困境与出路: 2025 – 2026 深度研究报告", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793775279&ver=7067&signature=1efKMQaZzt5fz9vm9WvzhOEJyX15xyNh6ItPq9twzC3AyR6n8*zZMFDryD43vwwcObPT11TB7ed5a2cy6ReBsdV02IpNn6QwQaRKhVsDYzADmUA4RF3-TuJa4YlcLlni&new=1", "added": "2026-11-04"}
{"title": "中国汽车 出海 潜在市场研究(附中国 车企海外 生产基地及 产能 情况梳理...", "url": "https://www.163.com/dy/article/JRDEVLNG0511IK8N.html", "added": "2026-11-04"}
{"title": "2025年 中国汽车 出口 销量:比亚迪第三,第十竟然是家韩国 车企 ?_车...", "url": "https://chejiahao.autohome.com.cn/info/19542552", "added": "2026-11-04"}
{"title": "2023年新能源 乘用车 行业 海外 出口 分析报告：风起云又涌,扬帆立潮头...", "url": "https://max.book118.com/html/2024/1030/8031073077006140.shtm", "added": "2026-11-04"}
{"title": "2023年中国 乘用车出口 市场总结与展望:乘用车出口高速增长, 车企出 ...", "url": "https://www.d1ev.com/news/shichang/226379", "added": "2026-11-04"}
{"title": "比亚迪 ( 002594 )_ 股票 行情_证券之星", "url": "http://stock.quote.stockstar.com/realtime_002594.shtml?t=4&frm=360", "added": "2026-11-04"}
{"title": "比亚迪 ( 002594 )_公司资料_证券之星", "url": "https://stock.quote.stockstar.com/corp_002594.shtml", "added": "2026-11-04"}
{"title": "比亚迪股票 _数据_资料_信息 — 东方财富网", "url": "http://data.eastmoney.com/stockdata/002594.html", "added": "2026-11-04"}
{"title": "比亚迪 ( 002594 )_ 股票 行情,行情首页_中财网", "url": "https://quote.cfi.cn/quote_002594.html", "added": "2026-11-04"}
{"title": "比亚迪 （ 002594 ）个股资金流向查询_个股行情_同花顺财经", "url": "http://stockpage.10jqka.com.cn/002594/", "added": "2026-11-04"}
{"title": "比亚迪 （ 002594 ）股吧_ 比亚迪 怎么样_分析讨论社区—东方财富网", "url": "http://gubapost.eastmoney.com/list,002594.html", "added": "2026-11-04"}
{"title": "比亚迪 （ 002594 ） - 两年内主要股东 - 股票 行情中心 - 搜狐证券", "url": "https://q.stock.sohu.com/cn/002594/zygd_2.shtml", "added": "2026-11-04"}
{"title": "比亚迪 （ 002594 ） 股票 股价 ,行情,新闻,财报数据_新浪财经_新浪网", "url": "https://finance.sina.com.cn/realstock/company/sz002594/5g.shtml", "added": "2026-11-04"}
{"title": "电池消费税 新调整", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793777803&ver=7067&signature=Z*I7HjG5DOXDSWCzjBshYlS9Ouq2aa6boO5OuPcT43*ujt311UJx9oj7T*Bm3KMaK2AAYBC8bMEPO42gwks1Lu3c527K1lZeDX7XqV-*TpfrUQLBDGq5EKUAE42xsMfK&new=1", "added": "2026-11-04"}
{"title": "锂电市场_锂电市场分析_锂 电池 市场前景分析 - OFweek锂电网", "url": "https://libattery.ofweek.com/CATList-36000-8400-libattery-7.html", "added": "2026-11-04"}
{"title": "电池消费税 新调整", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793777806&ver=7067&signature=Z*I7HjG5DOXDSWCzjBshYlS9Ouq2aa6boO5OuPcT43*ujt311UJx9oj7T*Bm3KMac5uO8yAQgqmxxOpZoguDB22bhcSTtqS595bX8xE8-50zpF9Er6UIEE2n-UTVEaXo&new=1", "added": "2026-11-04"}
{"title": "2024中国 车企盈利 榜出炉: 比亚迪 领跑,新势力何时扭亏为盈?-智能...", "url": "http://www.itbear.com.cn/html/2025-05/808585.html", "added": "2026-11-04"}
{"title": "比亚迪、吉利、长城 等120家知名 汽车企 业2023年业绩重磅出炉_财富...", "url": "https://caifuhao.eastmoney.com/news/20240507102316601428410", "added": "2026-11-04"}
{"title": "乘联分会： 2025年 1月 比亚迪 汽车 出口 66336辆,特斯拉中国 出口 ...", "url": "https://tech.ifeng.com/c/8gt7e0CVAUC", "added": "2026-11-04"}
{"title": "重磅！ 2025 第一季度 车企 出口 量： 奇瑞 25.4万辆, 上汽 第3,吉利第5|...", "url": "https://www.163.com/dy/article/JV4R7SBD0541B741.html?f=post1603_tab_news", "added": "2026-11-04"}
{"title": "1月车市销量：比亚迪、 吉利 等12家车企迎“开门红” 长城、 理想、赛 ...", "url": "https://www.sohu.com/a/858390189_430289", "added": "2026-11-04"}
{"title": "冰箱、沙发、大彩电就是好！ 理想 超 长城 成中国车企第二--快科技--科技...", "url": "https://news.mydrivers.com/1/914/914489.htm", "added": "2026-11-04"}
{"title": "乘联会 :2024年12月 特斯拉 在国内市场 份额 出现下滑| 特斯拉 _新浪财经...", "url": "https://finance.sina.com.cn/stock/relnews/us/2025-01-09/doc-ineekkkx3146886.shtml", "added": "2026-11-04"}
{"title": "乘联分会：2月 新势力 份额 19.9%,小米汽车等拉动 新势力 份额 同比增...", "url": "http://auto.cnfol.com/cheshidongtai/20250310/31190483.shtml", "added": "2026-11-04"}
{"title": "2025年 4月 乘用车 市场 降价 分析_搜狐汽车_搜狐网", "url": "https://www.sohu.com/a/893933002_115312", "added": "2026-11-04"}
{"title": "2025 年 乘用车 降价 潮持续,3月23款 车型 降价 力度各异_搜狐汽车_搜狐...", "url": "https://www.sohu.com/a/880452315_362225", "added": "2026-11-04"}
{"title": "2025年乘用车降价 力度仍强劲 纯电动 车型降幅 达9.1%- DoNews快讯", "url": "https://www.donews.com/news/detail/8/4960162.html", "added": "2026-11-04"}
{"title": "2025年 1月 乘用车 市场:价格结构如何影响销量格局?_搜狐汽车_搜狐网", "url": "https://www.sohu.com/a/859198404_362225", "added": "2026-11-04"}
{"title": "专家解读 | 2025年3 月 乘用车 价格段市场结构分析_搜狐汽车_搜狐网", "url": "https://www.sohu.com/a/883942825_253988", "added": "2026-11-04"}
{"title": "30万 以上 的车全国 占比 -今日头条", "url": "https://www.toutiao.com/topic/7474104545485817891/", "added": "2026-11-04"}
{"title": "一家 公司 「 值 多少钱」是怎么 估 出来的?", "url": "http://mp.weixin.qq.com/s?src=3&timestamp=1793801906&ver=1&signature=bZjBZ26NQLAwPb4KbVBACvATd9dhVPLT7R3Eun*ttFu6wyUiH0nz1xQJrvq6pipiGTr4SLcC78rLxJAqT2l6E31WHVc5Jlw9UCjVhxvNOuq3bk0XiCdAKswPz*cCt4ADH8kejj947CbPaxOMQkqKNA==", "added": "2026-11-04"}
{"title": "2025年我国 新能源乘用车 市场 渗透率 有望达到57%_天天基金网", "url": "http://fund.eastmoney.com/a/202501103293633145.html", "added": "2026-11-04"}
{"title": "2024年12月国内 新能源车零售渗透率 近50%| 乘用车 | 豪华车 |中国汽车流...", "url": "https://www.163.com/dy/article/JLFPR1QJ0519DFFO.html", "added": "2026-11-04"}
{"title": "高盛解读中国 乘用车 市场 2025年 11月分析:新能源渗透率达59.3%_哔...", "url": "https://www.bilibili.com/video/BV1MJmiBAEwG/", "added": "2026-11-04"}
{"title": "2025 年车市未现“开门红”,1月 乘用车 销量同比下降12%_凤凰网", "url": "https://finance.ifeng.com/c/8guxo9ZBZE9", "added": "2026-11-04"}
{"title": "2025年 4月 乘用车批发 219万辆 同比增10.7% 批发格局持续变化 小企业...", "url": "https://www.sohu.com/a/894077349_430289", "added": "2026-11-04"}
{"title": "乘 联分会: 2025年 4月 自主品牌零售 同比增长31% 份额 达65.5% | 互联...", "url": "http://www.199it.com/archives/1755278.html", "added": "2026-11-04"}
{"title": "2024年我国 乘用车 零售 超2289万辆 乘联分会： 2025 年新能源 乘用车 市...", "url": "https://news.qq.com/rain/a/20250109A08IZR00", "added": "2026-11-04"}
{"title": "越过2024| 自主品牌 全年 乘用车 市场 份额 超过六成_汽车圈_澎湃新闻-...", "url": "https://www.thepaper.cn/newsDetail_forward_29903357", "added": "2026-11-04"}
{"title": "2024年我国 乘用车零售 超2289万辆 乘联分会: 2025年 新能源乘用车市...", "url": "https://www.nbd.com.cn/articles/2025-01-09/3716230.html", "added": "2026-11-04"}
{"title": "2025年 1月全国 乘用车 市场 零售 下降12.1%,自主品牌 份额 再创新高_...", "url": "https://www.sohu.com/a/857890566_122006510", "added": "2026-11-04"}
{"title": "2025年 4月中国 车 企 零售 销量排名公布 仅大众出现下跌| 乘用车 |大众汽...", "url": "https://www.163.com/dy/article/JVH0FTI3051191D6.html", "added": "2026-11-04"}
{"title": "碳酸锂 现货价格 指数", "url": "https://www.mysteel.com/mmlc", "added": "2026-11-04"}
{"title": "碳酸锂 现货价格 再走“下坡路”跌破9万元/吨_锂_稀贵金属_91金属网", "url": "https://news.91jinshu.com/li/202407/68980.html", "added": "2026-11-04"}
{"title": "2025年碳酸锂 市场动态分析: 价格 反弹中的低价库存挑战_电池_..._ 现货", "url": "https://www.sohu.com/a/882204307_122066678", "added": "2026-11-04"}
{"title": "重卡行业 10月跟踪月报：景气度底部已现-研究报告正文 _ 数据中心 _ ...", "url": "http://data.eastmoney.com/report/zw_industry.jshtml?infocode=AP202411261641066600", "added": "2026-11-04"}
{"title": "不同能源 类型 中 重卡 市场前景分析(上)—油气电", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793804023&ver=7068&signature=zeBTFj4IAA-ozAqO3aCPQhCmdYoR4exv1to7s51rWC0xDPnmRrUXyR4IhuBuXGjkJZCd37M8ImQ*77bMviVGccs1nPTce-7vIh1DO4mKNs3RxZXqs5tbBiMs9Iq-9uiD&new=1", "added": "2026-11-04"}
{"title": "2024年一季度 客车 市场现状分析", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793804054&ver=7068&signature=LOG1P9hrXcuL40YdOOXLgP59YvUCh1AY34RxWQ5MBMl1vFXC-4HN8iAKZ*y8Zaj3Y8fSHRb4JYAip1IHrxJlFn1kVNbUak*LbcrUq4cwG4ei3DrCjBB23dVKG54B1EtX&new=1", "added": "2026-11-04"}
{"title": "客车 观察丨2024年 客车出口 市场累 销 6.2万辆,创近5年新高_腾讯新闻", "url": "https://news.qq.com/rain/a/20250128A01VW700", "added": "2026-11-04"}
{"title": "客车 12 月 月报:11 月国内公交 交付爆发 翘尾效应初现__新浪财经_新浪...", "url": "http://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/787933354036/index.phtml", "added": "2026-11-04"}
{"title": "2026年 9月中国商用车 出口 市场扫描简报", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793804091&ver=7068&signature=O41e8g87x3B6swO6pclfxGT6M3T1HKI3F6xsw*m8qHRfL7RoDdLOS6Syf7Wtkf1RFXufzvPYpnLGQ7b3ncDgXwtFwZ80pAaTFgAwg34qMhP2i57EfME8C8ZFpbH9BnW6&new=1", "added": "2026-11-04"}
{"title": "宇通客车 ：2024 年度利润分配 方案及 2025 年中期 分红 授权的 公告 _ 宇通 ...", "url": "http://gubapost.eastmoney.com/news,600066,1534566996.html", "added": "2026-11-05"}
{"title": "【周观点】坚定看好!", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793811189&ver=7068&signature=i5*A*5fRKl40IBxYxva44S*rwi03WUMePqsMTjz*viFejjvtCUYR2hQ0pc0mmQlEA*A2A3eWGynDnsu1SDMArQl4XA0HjbJY7ZOzzvyehMH9-z4jprtnUsBKJbhCI6A0&new=1", "added": "2026-11-05"}
{"title": "2026 深圳 汽车 以旧换新 购车 补贴 申请条件- 深圳本地宝", "url": "http://sz.bendibao.com/jt/2024525/973993.htm", "added": "2026-11-05"}
{"title": "2026年 10月27日全国 货运车补贴 申领全攻略, 营运货车 车主必读", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793811300&ver=7068&signature=WOzMEAC79yVxc1QI2WDpbSlSDNnojfMHwiLHe4MrRrXeyz8Vj4ZrmcUBWBDfhG3yoBwvU98-*1FP-7LJ4*u3fe8AwFY3GGUZiksfVy3QdjULR7T4T-JL0aq9E3tvqkwS&new=1", "added": "2026-11-05"}
{"title": "宇通 客车 （600066）_公司资料_证券之星", "url": "https://stock.quote.stockstar.com/corp_600066.shtml", "added": "2026-11-05"}
{"title": "【整车主线周报】多家车企三季报公布,本周SW商用载 客车 表现...", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793811511&ver=7068&signature=i5*A*5fRKl40IBxYxva44S*rwi03WUMePqsMTjz*viFPTvbqBw2y8zE0KZirBFH1PdHRdagg3bE0ekZ2RLJUsKGPWx2Cs2PiWvjzYUaAGlp985S3UF7jxEwPZ6ejKCbN&new=1", "added": "2026-11-05"}
{"title": "宇通客车 股份有限公司 2025 年4月份产销数据快报| 宇通客车 _新浪财经_...", "url": "https://finance.sina.com.cn/roll/2025-05-08/doc-inevuvpn0618647.shtml", "added": "2026-11-05"}
{"title": "宇通客车 (600066) - 分红送配 记录 - 股票行情中心 - 搜狐证券", "url": "https://q.stock.sohu.com/cn/600066/fhsp.shtml", "added": "2026-11-05"}
{"title": "金龙 汽车 (600686)投资评级,金龙 汽车 (600686)最新评级_证劵...", "url": "https://stock.quote.stockstar.com/corp/rating_600686.shtml", "added": "2026-11-05"}
{"title": "宇通客车 : 2024 年度利润分配 方案及 2025年 中期 分红 授权的 公告 内容...", "url": "https://stock.stockstar.com/AN2025040100035728.shtml", "added": "2026-11-05"}
{"title": "【整车主线周报】重卡9月数据公布,本周SW 汽车 零部件表现较好", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793864520&ver=7069&signature=Ah8baAdCJGklUkTce-dOWZBbJuy6ckAZV7h*BAZq*Js2eb9JatVMXiXMuPau-PEKbv6-ECEUNj-zD5OAjEavpvW15C9wqaYwOkHmXdDfJVCvqkiIoP6RQ6QxlilgbhaR&new=1", "added": "2026-11-05"}
{"title": "【整车主线周报】重卡9月数据公布,本周SW 汽车 零部件表现较好", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793864522&ver=7069&signature=Ah8baAdCJGklUkTce-dOWZBbJuy6ckAZV7h*BAZq*Js2eb9JatVMXiXMuPau-PEK4GiouiZgE7kZL9wT3SUHowfonZmvx3vRuHdlxz924JOuzg7zjcsbFPxXPZxNCtsO&new=1", "added": "2026-11-05"}
{"title": "基金重仓前100名市盈率、市净率_ 指数 _乐咕乐股网", "url": "https://legulegu.com/stockdata/index-basic?indexCode=800100.LG", "added": "2026-11-05"}
{"title": "指数 估值日报20261103", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793864560&ver=7069&signature=8ey-RUwxm3gbcSIQ5yfI61kOTTyX-c1PsHnnLjdXBbTFrhMEnoLD53RUMZti8yk-vt1T7u3zV3G2hLQJhp4jgJum0gCdp*N69la02sp4-S8EvfPEqh40fDfu6xUvEyL6&new=1", "added": "2026-11-05"}
{"title": "汽车 行业跟踪周报：5月第五周乘用车批发销量环比+62%,优选【出海...", "url": "https://stock.stockstar.com/JC2023061200003022.shtml", "added": "2026-11-05"}
{"title": "指数 估值（ 2026 -10-29）", "url": "https://danjuanfunds.com/djmodule/value-center", "added": "2026-11-05"}
{"title": "指数 估值（ 2026 -09-09）", "url": "https://danjuanapp.com/djmodule/value-center?channel=1300100141", "added": "2026-11-05"}
{"title": "宇通客车股份有限公司 2024年年度利润分配 实施 公告 _现金 红利 _全体_...", "url": "https://www.sohu.com/a/893023205_120988533", "added": "2026-11-05"}
{"title": "【重磅深度】 客车 系列深度三: 客车出口 年度复盘及展望", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793871222&ver=7070&signature=2Z3gK0PNNHe2UYojHvvrF8J7RlUR8*ecN0WGoJnYvUsKzOKCj*P-pChJ4KSJD7Rm20G2E6QVxURQ*U0rK9I-1kr31INu-GzM7Z3SRFF8vb1*tbG0sURgH59aczGtZPzK&new=1", "added": "2026-11-05"}
{"title": "【整车主线周报】多家车企三季报公布,本周SW商用载 客车 表现...", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793871262&ver=7070&signature=2Z3gK0PNNHe2UYojHvvrF8J7RlUR8*ecN0WGoJnYvUvN-IpJLHULFRSVTHUW1rCTUHxTZnhOvrRHbYg3TDA7wGxo10X6FqXFQk6Cm1WFIEIcdqeJEPXnRomZhTvaeXp0&new=1", "added": "2026-11-05"}
{"title": "客车 观察丨 2025 年我国大中型 客车 增长的机会在哪里？_腾讯新闻", "url": "https://news.qq.com/rain/a/20250207A064V700", "added": "2026-11-05"}
{"title": "汽车 市盈率、市净率_ 申万 行业_乐咕乐股网", "url": "https://legulegu.com/stockdata/sw-industry-2021?industryCode=801880.SI", "added": "2026-11-05"}
{"title": "交通运输市盈率、市净率_ 申万 行业_乐咕乐股网", "url": "https://legulegu.com/stockdata/sw-industry-2021?industryCode=801170.SI", "added": "2026-11-05"}
{"title": "城商行Ⅱ市盈率、市净率_ 申万 行业_乐咕乐股网", "url": "https://legulegu.com/stockdata/sw-industry-2021?industryCode=801784.SI", "added": "2026-11-05"}
{"title": "汽车 服务市盈率、市净率_ 申万 行业_乐咕乐股网", "url": "https://legulegu.com/stockdata/sw-industry-2021?industryCode=801092.SI", "added": "2026-11-05"}
{"title": "2026年11月 4日指数基金估值", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793871375&ver=7070&signature=FdLr-5OG42u*NcPWMxmJ1NzSu8a3EvT*J-Nrrvi2UOE6ww4zHl6HnmYsMKBr6gJSuTQGq2aw7491RIi5WanNh*fYx6nxy9WPgF*o-7QtsFtYs1Au*F4Qd-wgygBQFCbN&new=1", "added": "2026-11-05"}
{"title": "4 月1 日 股市必读: 宇通客车 年报 - 第四季度单季净 利润 同比..._ 年度 _报告", "url": "https://www.sohu.com/a/878490680_115377", "added": "2026-11-05"}
{"title": "交通运输部 国家发展改革委 财政部关于实施 老旧营运货车报废更新 的...", "url": "https://www.gov.cn/zhengce/zhengceku/202503/content_7014455.htm", "added": "2026-11-05"}
{"title": "2026车辆报废补贴标准 是多少钱?-法律知识|律图", "url": "https://www.64365.com/zs/1028014.aspx", "added": "2026-11-05"}
{"title": "2026年 10月27日全国货运车 补贴 申领全攻略, 营运货车 车主必读", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793876562&ver=7070&signature=v50xFb-FO6TVzPPVa6hZe*bx2BFRvjdjoSIqg-hLKHp7CRmpomxE40ydPY8-jJ*IrrbZOOzLh3hZLOYH4uv82fKZeKzYxl75JHggOGLUbYOE6hCU2MLyPDaJ9RuHb*Zo&new=1", "added": "2026-11-05"}
{"title": "2026年 10月12日全国 货车 补贴 申领全攻略, 营运货车 车主必读", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793876559&ver=7070&signature=v50xFb-FO6TVzPPVa6hZe*bx2BFRvjdjoSIqg-hLKHrJyg*WfypE8Ha0621TFcYkMJDBvz4vYmbQaypKedkECVIgtmTQaKwMyIIpYWawwSf8AzOdb9CT-hCBjM0NMLak&new=1", "added": "2026-11-05"}
{"title": "【提醒】我省 2026年度老旧营运货车报废更新 细则即将落地,请广大车...", "url": "https://jtt.ln.gov.cn/jtt/xw/tzjg/2026051517233645430/index.shtml", "added": "2026-11-05"}
{"title": "白酒,终于等到拐点了?", "url": "https://new.qq.com/rain/a/20261102A04JUK00", "added": "2026-11-05"}
{"title": "批价 跌了别急着清库,先算这三个数", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793893313&ver=7070&signature=KBqlylssWuk7pW8RZeOqNu69ebP5szKcz2q2oeSDZf58NYEtU1il9K3voUmh1nrAjff5NNU3wuoR70Z5vEuwNvmkSneHxA-0wwHm1nVp*woCzDFsvPBcELmeFDp8OF9e&new=1", "added": "2026-11-05"}
{"title": "每日A股晨报· 2026年 10月26日(周一)", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793893808&ver=7070&signature=2Lm-oUKPcqfuUAxRlhx3lYH8JowRmTifigiXgDoeQtIC3K9H5gKs78GAtsjdxKWxz2s0*j6VRk4xSbNlrFmJeov0tnDOLBcI3p33-YlDZhT25yKm-98XEllnZ3DxjrYE&new=1", "added": "2026-11-05"}
{"title": "基金重仓 行业排名 - 申万1级2026年 第2季度(更新中)_乐咕乐股网", "url": "https://legulegu.com/stockdata/fund-sw-industry-holding?level=1", "added": "2026-11-05"}
{"title": "今日酒价 - 国窖1573价格 下跌各大名酒批发参考价 10月 22日", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793896398&ver=7070&signature=Jw9DpDY4Fc5ecC2w*fmmZDQEKdh*-DrL7tM4HUmHNTo89Lpqcp1GknB3aNHoxFS*kXD9FGUINK0n8dA9-LGYxxGxHcMbnT5aW7fWpoJZWgk3mO0FqltAqhsq*W1*PkrM&new=1", "added": "2026-11-06"}
{"title": "今日酒价 - 国窖1573 价格下跌各大名酒批发参考价 10月 22日", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793896401&ver=7070&signature=2JLtmTGINQYP0G*3lybyQXVYxkBvrjY89My5o2P52xRAF*NRMew6pjy4WYRoHeqZ07nZe3HMs7yRGxMqxNedacp59vkQdJU20vKNij86RCamMhy9u9j2l4v8v4GwwSg5&new=1", "added": "2026-11-06"}
{"title": "食品饮料 行业2024 年 四季度 基金持仓 分析: 白酒 超 配比例 收窄,大众品...", "url": "http://data.eastmoney.com/report/zw_industry.jshtml?infocode=AP202501241642520367", "added": "2026-11-06"}
{"title": "食品饮料 Q3 基金持仓 数据: 白酒 获小幅增持,板块仓位环比回升", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793897138&ver=7070&signature=v8vM70cIm2xPQYntrP*nKwy4tPm-KSEWsejBWlCkw2PUbcMqZLUwZAojWennFR43XvZ7fG6WYhkFfm27YM8bCu3CWqEwFww1Su0HX4v6T7QKmRo*Tq831tLkLDcvThzy&new=1", "added": "2026-11-06"}
{"title": "食品饮料 Q3 基金持仓数据 : 白酒 获小幅增持,板块仓位环比回升", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793897140&ver=7070&signature=v8vM70cIm2xPQYntrP*nKwy4tPm-KSEWsejBWlCkw2PUbcMqZLUwZAojWennFR43r1DPvVZKLw0JfXqL-FvFtaIMic-QtIMkz0z2hFBZFZ0tnHfpTTRHRAxJhQnZG4dE&new=1", "added": "2026-11-06"}
{"title": "食品饮料 Q3 基金持仓 数据: 白酒 获小幅增持,板块仓位环比回升", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793897143&ver=7070&signature=v8vM70cIm2xPQYntrP*nKwy4tPm-KSEWsejBWlCkw2PUbcMqZLUwZAojWennFR43ar5bEHTSgVR*-X9R5h-FEmSQIBPZkFag8fnqjWk7HXNTGeJEE6HQ3aoPbF0lLPK5&new=1", "added": "2026-11-06"}
{"title": "贵州茅台 深度研究:营收1333亿守住正增长, 估值 却落进 近十年 极...", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793960790&ver=7072&signature=xxK3pdRcwX0k-h2tCDelYMefJ-P4WuPQfeDxoxXFWYX2ZtT*NUx1Ywur9Oosktqt*JOTMWq9MGViNz*1jRzL6E4qmP9RyLARfmymTnihHcg6A-szJlALEqKZ*hKE7I8t&new=1", "added": "2026-11-06"}
{"title": "贵州茅台 ( 600519 ) 估值 分析_数据中心_东方财富网", "url": "http://data.eastmoney.com/gzfx/detail/600519.html", "added": "2026-11-06"}
{"title": "散飞行情下跌; 今日 名酒行情价格 2026 . 11 .06", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793960842&ver=7072&signature=W*P2jWJdhcvjxGyW3hdol*Yne7mUz2pp1AC6fLBjfBW8uNCMjhy4Nh-p5rFKIaVy*BFieu14ax-Vfhl*pblXDma7LR1V29ulZiVJDTS9yszUYcq3kU5WgssXtFejq-8n&new=1", "added": "2026-11-06"}
{"title": "贵州茅台 深度研究:营收1333亿守住正增长, 估值 却落进近十年极...", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793973005&ver=7072&signature=xxK3pdRcwX0k-h2tCDelYMefJ-P4WuPQfeDxoxXFWYUJN8SHwNBVIMP0yjq7HVJe28rqMD-8zef9A0*9rlxZk0e4EhjVZPzfpiypmcsUPSpoVHmlZ2eLAQOrZYCsRsqJ&new=1", "added": "2026-11-06"}
{"title": "茅台酒今日 行情 价格 一览( 2026年10月 31日) 飞天散瓶 原箱 批价 ...", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793973105&ver=7072&signature=P7rm*V*bxQUbZeSj6tvs8KXRBCWiHFG1u7eDahPLRv9Y8ZxjK6zA-QNirFpdEo4Cfn6HjUBaBSq92fDoIn8hAbXCEha2m5wvx48RYbqmwRwD*oJz79xYZlMA0BhxfGuT&new=1", "added": "2026-11-06"}
{"title": "五粮液 ( 000858 )_股票行情,行情首页_中财网", "url": "https://quote.cfi.cn/quote.aspx?stockid=380&lm=same_hy&client=pc", "added": "2026-11-06"}
{"title": "五粮液 （ 000858 ）股票股价,行情,新闻,财报数据_新浪财经_新浪网", "url": "https://finance.sina.com.cn/realstock/company/sz000858/5g.shtml", "added": "2026-11-06"}
{"title": "五粮液 ( 000858 )估值分析_数据中心_东方财富网", "url": "https://data.eastmoney.com/gzfx/detail/000858.html", "added": "2026-11-06"}
{"title": "五粮液 （SZ 000858 ）股票股价_股价行情_财报_数据报告 - 雪球", "url": "https://xueqiu.com/S/SZ000858/news?page=58", "added": "2026-11-06"}
{"title": "五粮液 （ 000858 ）_股票行情_证券之星", "url": "http://stock.quote.stockstar.com/realtime_000858.shtml?t=5&frm=360", "added": "2026-11-06"}
{"title": "贵州茅台 市盈率（ PE ）(600519.SH)- 小乐财报", "url": "https://leetab.com/stock/sh600519/pe", "added": "2026-11-06"}
{"title": "TOP3: 五粮液 (000858)今日( 2026年 10月30日)主力净流入...", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793980659&ver=7072&signature=zMZX90pbnwyRrQFnJ-NK5nSJJdYPQL3P21MR1ifVYmAAWPVKREbIySwA3vp6Jj3ZrQBJSciKLwSu7MPMmPdDjsIEMHSW-gjGUPCdXd1cmKxdjVO8YBdSZX-Hqi9EoV4*&new=1", "added": "2026-11-06"}
{"title": "五粮液 (SZ 000858 )股票股价_股价行情_财报_ 数据 报告 - 雪球", "url": "https://xueqiu.com/S/SZ000858", "added": "2026-11-07"}
{"title": "TOP3: 五粮液 (000858)今日( 2026年 10月30日)主力净流入...", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793981880&ver=7072&signature=zMZX90pbnwyRrQFnJ-NK5nSJJdYPQL3P21MR1ifVYmAAWPVKREbIySwA3vp6Jj3ZQ-4nHh7XBbQLJUSVsozRarKTXfQpoTVZ2OPGbbWGyreWENeIp0RPGUXoFdgD8whN&new=1", "added": "2026-11-07"}
{"title": "泸州老窖 ( 000568 ) 估值 分析_数据中心_东方财富网", "url": "http://data.eastmoney.com/gzfx/detail/000568.html", "added": "2026-11-07"}
{"title": "泸州老窖 ( 000568 ) 最新价格 _行情_走势图—东方财富网", "url": "http://quote.eastmoney.com/concept/sz000568.html?from=classic", "added": "2026-11-07"}
{"title": "泸州老窖 ( 000568 )_股票行情,行情首页_中财网", "url": "http://quote.cfi.cn/(X(1)S(garseyvgtj1np04501gbik55))/quote_000568.html", "added": "2026-11-07"}
{"title": "山西汾酒 ( 600809 ) 估值 分析_数据中心_东方财富网", "url": "https://data.eastmoney.com/gzfx/detail/600809.html", "added": "2026-11-07"}
{"title": "山西汾酒 ( SH600809 )股票股价_股价行情_财报_数据报告 - 雪球", "url": "https://xueqiu.com/S/SH600809", "added": "2026-11-07"}
{"title": "山西汾酒 ( 600809 )_股票行情,行情首页_中财网", "url": "http://www.eye.net.cn/quote.aspx?zqlb=0&stockcode=600809&lx=%e8%af%84%e8%ae%ba", "added": "2026-11-07"}
{"title": "古井贡酒 ( 000596 ) 估值 分析_数据中心_东方财富网", "url": "https://data.eastmoney.com/gzfx/detail/000596.html", "added": "2026-11-07"}
{"title": "古井贡酒 (SZ 000596 )股票股价_股价行情_财报_数据报告 - 雪球", "url": "https://xueqiu.com/S/SZ000596", "added": "2026-11-07"}
{"title": "2026年10月 25 日 市面常见 白酒价格 全览: 茅台 至剑南春等品牌价...", "url": "http://mp.weixin.qq.com/s?src=11&timestamp=1793983743&ver=7072&signature=d4EGQe-q4y-pOsXjC7l8icKhCfKJ0GwznaU1Ej-1dtgDuruxV1wHMQ9V-EFYA43piW*ruVgmpcE2kBcRQ7dc5rtBlloaiinxe*VLYebPZdivaKErS2t31Y*Z6IsyrhgW&new=1", "added": "2026-11-07"}
{"title": "酒排名 - 白酒选购与 今日酒价 ,帮你买对少花冤枉钱", "url": "https://www.jiupaiming.com/", "added": "2026-11-07"}
{"title": "中国10年期国债 到期 收益率 与沪深300指数 - value500投资导航", "url": "http://value500.com/10Bond.html", "added": "2026-11-07"}
<!-- SOURCES:WEB:END -->
