"""
D2 REST 端点『无凭据』侦察脚本（M0，只读 GET，不需要密码）
================================================================
判断 D2 主机（config.ini 的 [d2] host）上到底有没有 Documentum / D2 REST 组件、用什么认证方式，
从而决定走主路（REST+DQL）还是备路（Playwright 浏览器）。

* 纯 Python 标准库，无需 pip 安装任何东西（系统 Python 直接跑）。
* 全部是匿名 GET，**不涉及任何凭据**，不会改动服务端任何东西。
* 不跟随重定向 —— 好让我们看清 REST 端点是回 401（可直连认证）
  还是 30x 跳到 SSO 登录页（被单点登录接管）。

用法：  python probe_recon.py
输出：  控制台摘要 + probe_out/recon_http.json（已被 .gitignore 挡住，不上传）
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path

from d2conf import load_d2

HOST = load_d2()[0]  # 内网主机名从 config.ini 读，不硬编码进公开代码
OUT = Path(__file__).parent / "probe_out"
TIMEOUT = 15

# Documentum REST / D2 REST / CMIS 的常见部署路径，逐个匿名试探
CANDIDATES = [
    f"https://{HOST}/dctm-rest/",
    f"https://{HOST}/dctm-rest/repositories",
    f"https://{HOST}/dctm-rest/product-info",
    f"https://{HOST}/D2-REST/",
    f"https://{HOST}/D2-REST/repositories",
    f"https://{HOST}/D2-REST/product-info",
    f"https://{HOST}/D2-REST/services",
    f"https://{HOST}/documentum-rest/repositories",
    f"https://{HOST}/emc-rest/repositories",
    f"https://{HOST}/cmis",
    f"https://{HOST}/emc-cmis/resources",
    f"https://{HOST}/D2/",
    f"http://{HOST}:8080/dctm-rest/repositories",
    f"http://{HOST}:8080/D2-REST/repositories",
]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """阻止自动跟随 3xx，把重定向当作 HTTPError 抛出，便于看清 Location。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def make_opener() -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # 内网自签证书
    return urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ctx))


def probe(opener: urllib.request.OpenerDirector, url: str) -> dict:
    req = urllib.request.Request(
        url, method="GET",
        headers={"Accept": "application/json", "User-Agent": "D2-recon/1"},
    )
    try:
        r = opener.open(req, timeout=TIMEOUT)
        body = r.read(1200).decode("utf-8", "replace")
        return {
            "url": url, "status": r.status,
            "www_auth": r.headers.get("WWW-Authenticate"),
            "location": r.headers.get("Location"),
            "content_type": r.headers.get("Content-Type"),
            "snippet": " ".join(body.split())[:300],
        }
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(1200).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        h = e.headers
        return {
            "url": url, "status": e.code,
            "www_auth": h.get("WWW-Authenticate") if h else None,
            "location": h.get("Location") if h else None,
            "content_type": h.get("Content-Type") if h else None,
            "snippet": " ".join(body.split())[:300],
        }
    except Exception as e:  # noqa: BLE001
        return {"url": url, "status": -1, "error": f"{type(e).__name__}: {e}"}


def classify(rec: dict) -> str:
    s = rec.get("status")
    if s == 200:
        return "ANON-OK"        # 匿名可读，服务确定在（少见）
    if s == 401:
        return "NEEDS-AUTH"     # 服务在、需认证 —— 主路好信号
    if s == 403:
        return "FORBIDDEN"      # 服务在，但权限/方法被拦
    if s in (301, 302, 303, 307, 308):
        loc = (rec.get("location") or "").lower()
        if any(k in loc for k in ("login", "sso", "saml", "adfs", "auth", "sign")):
            return "SSO-REDIRECT"   # 被单点登录接管 —— 主路坏信号
        return "REDIRECT"
    if s == 404:
        return "NOT-FOUND"      # 该路径无此服务
    if s == -1:
        return "DEAD"           # 连不上
    return f"HTTP-{s}"


def main() -> int:
    OUT.mkdir(exist_ok=True)
    opener = make_opener()
    rows = []
    print(f"侦察 {HOST}（匿名 GET，无需密码）...\n")
    print(f"{'verdict':<13} {'status':>6}  {'www-authenticate':<24} url")
    print("-" * 96)
    for url in CANDIDATES:
        rec = probe(opener, url)
        rec["verdict"] = classify(rec)
        rows.append(rec)
        wa = (rec.get("www_auth") or "")[:24]
        print(f"{rec['verdict']:<13} {str(rec['status']):>6}  {wa:<24} {url}")
    (OUT / "recon_http.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    alive = [r for r in rows if r["verdict"] in ("ANON-OK", "NEEDS-AUTH", "FORBIDDEN")]
    sso = [r for r in rows if r["verdict"] == "SSO-REDIRECT"]
    print("\n=== 结论 ===")
    if alive:
        print(f"发现 {len(alive)} 个活着的 REST 端点：")
        for r in alive:
            print(f"   [{r['verdict']}] {r['url']}  auth={r.get('www_auth')}")
        low = lambda r: (r.get("www_auth") or "").lower()  # noqa: E731
        basic = [r for r in alive if low(r).startswith("basic")]
        nego = [r for r in alive if any(k in low(r) for k in ("negotiate", "ntlm"))]
        print()
        if basic:
            print("   -> 支持 Basic Auth：主路可行。下一步带账号密码实测 /repositories 与 DQL。")
        if nego:
            print("   -> 只给 Negotiate/NTLM：Basic 不行，需 Windows 集成认证（requests-negotiate-sspi）。")
        if not basic and not nego:
            print("   -> 未明示认证方式，带凭据实测即可（多半是 Basic）。")
    elif sso:
        print("REST 端点被 SSO 登录页接管：Basic Auth 直连大概率不行。")
        print("考虑携带浏览器登录态（storage_state），或直接走备路 Playwright。")
    else:
        print("没有发现任何活着的 REST 端点 —— REST 组件很可能未部署，走备路（Playwright）。")
    print("\n结果已存 probe_out/recon_http.json（该目录已被 .gitignore 挡住，不会上传）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
