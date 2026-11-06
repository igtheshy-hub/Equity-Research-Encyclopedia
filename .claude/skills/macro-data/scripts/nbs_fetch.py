#!/usr/bin/env python3
"""国家统计局新 API 取数辅助（2026-09 实测版，旧 easyquery 已废止）。

用法：
  python3 nbs_fetch.py search <中文关键词>
  python3 nbs_fetch.py data <cid> <indicatorId> <dts>
    dts 示例：202601MM-202608MM（月）/ 202501SS-202602SS（季）/ 2020YY-2025YY（年）
"""
import json, ssl, subprocess, sys, time, urllib.request, urllib.parse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows 管道默认 GBK，中文会乱码

BASE = "https://data.stats.gov.cn/dg/website/publicrelease/web/external"
AREA = [{"text": "全国", "value": "000000000000"}]
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

def _fetch(url, body=None):
    # urllib 优先；SSL 失败时退 curl——本机开 Clash 系统代理时 stats.gov.cn 会被塞
    # MITM 证书报 SSLError，curl.exe（Schannel）不读系统代理，直连证书正常
    # （2026-10-24 Win10 实测；平日 VPN 关闭时 urllib 直连即成功，此为兜底）
    if body is None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except ssl.SSLError:
            p = subprocess.run(["curl", "-s", "-A", _UA, url], capture_output=True, timeout=30)
            if p.returncode == 0:
                return p.stdout.decode("utf-8", "replace")
            raise
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", "replace")
    except ssl.SSLError:
        p = subprocess.run(["curl", "-s", "-A", _UA, "-X", "POST", "-H", "Content-Type: application/json",
                            "-d", body.decode(), url], capture_output=True, timeout=30)
        if p.returncode == 0:
            return p.stdout.decode("utf-8", "replace")
        raise

def _get(url):
    # 服务端对 Python-urllib 默认 UA 不稳定（实测全败），必须显式带 UA；偶发异常 HTML 页，重试 3 次
    for attempt in range(3):
        try:
            raw = _fetch(url)
        except urllib.error.URLError as e:
            print(f"!! 第{attempt+1}次网络错误: {e}", file=sys.stderr)
            time.sleep(1 + attempt)
            continue
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            print(f"!! 第{attempt+1}次非JSON，前120字节: {raw[:120]!r}", file=sys.stderr)
            time.sleep(1 + attempt)
    print("!! 重试 3 次均失败；检查关键词是否为中文，或服务端维护中", file=sys.stderr)
    sys.exit(2)

def search(kw):
    # 注意：pageSize>=10 会触发服务端异常 HTML 页（2026-09 实测），必须用 <=5
    res = _get(f"{BASE}/query?search={urllib.parse.quote(kw)}&pagenum=1&pageSize=5")
    # 实测结构：{"data": {"types": [...], "data": [...]}}；偶发返回服务端异常 HTML 页，重试即可
    items = res.get("data")
    if isinstance(items, dict):
        items = items.get("data") or items.get("list") or []
    for item in items or []:
        print(json.dumps({k: item.get(k) for k in ("show_name", "dt_name", "value", "indic_id", "i", "treeinfo_globalid") if k in item}, ensure_ascii=False))

def data(cid, indicator_id, dts):
    body = json.dumps({
        "cid": cid, "indicatorIds": [indicator_id], "das": AREA,
        "dts": [dts], "showType": "1",
    }).encode()
    res = json.loads(_fetch(f"{BASE}/stream/esData", body=body))
    for row in res.get("data", []):
        for v in row.get("values", []):
            val = v.get("value") or "（未发布）"
            print(f"{row.get('name', row.get('code'))}\t{v.get('i_showname', '').strip()}\t{val} {v.get('du_name','')}")

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "search":
        search(sys.argv[2])
    elif len(sys.argv) >= 5 and sys.argv[1] == "data":
        data(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print(__doc__)
        sys.exit(1)
