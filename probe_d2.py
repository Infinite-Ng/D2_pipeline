r"""
D2 站点探测脚本（第 1 步，只读，不发任何邮件）
=================================================

作用：打开 D2 站点，让你手动登录一次，然后把
  1) 登录页的表单结构
  2) 登录后任务列表页面的 DOM
  3) 页面发出的所有 XHR/fetch 请求和响应体（ExtJS 的 grid store 接口就在这里面）
全部导出到 probe_out\ 目录，供后续写正式抓取脚本使用。

用法（在 D:\codingSpace\D2_agent 下打开 PowerShell 或 CMD）：
    python -m pip install playwright
    python -m playwright install chromium      # 若公司网络装不上，见 README 的 channel="chrome" 说明
    python probe_d2.py

注意：
* 脚本不会记录你输入的密码；登录由你在弹出的浏览器窗口里手动完成。
* 导出的 JSON 里包含你看到的业务数据（国家、主题等），属于内部信息，
  回传给我之前你可以先自己看一眼 probe_out\requests.jsonl。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from d2conf import load_d2

D2_URL = f"https://{load_d2()[0]}/D2/#d2"  # 内网主机从 config.ini 读，不硬编码
OUT = Path(__file__).parent / "probe_out"
MAX_BODY = 200_000  # 单个响应体最多保存 200KB

INTERESTING = re.compile(r"(json|xml|javascript|text/plain)", re.I)
SECRET_KEYS = re.compile(r"(password|passwd|pwd|secret|token|authorization)", re.I)


def scrub(text: str) -> str:
    """粗暴地把疑似密码字段的值遮掉，避免凭据落盘。"""
    return re.sub(
        r'("?(?:password|passwd|pwd|secret|token)"?\s*[:=]\s*"?)([^"&,}\s]+)',
        r"\1***REDACTED***",
        text,
        flags=re.I,
    )


def main() -> int:
    OUT.mkdir(exist_ok=True)
    records: list[dict] = []

    with sync_playwright() as p:
        # ignore_https_errors=True: 内网自签证书常见
        try:
            browser = p.chromium.launch(headless=False, channel="chrome")
        except Exception:
            browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1500, "height": 950})
        page = ctx.new_page()

        def on_response(resp):
            try:
                req = resp.request
                if req.resource_type not in ("xhr", "fetch", "document", "other"):
                    return
                ctype = (resp.headers or {}).get("content-type", "")
                rec = {
                    "method": req.method,
                    "url": resp.url,
                    "status": resp.status,
                    "resource_type": req.resource_type,
                    "content_type": ctype,
                    "request_headers": {
                        k: ("***REDACTED***" if SECRET_KEYS.search(k) else v)
                        for k, v in (req.headers or {}).items()
                    },
                    "post_data": scrub(req.post_data or "")[:20_000],
                    "body": None,
                }
                if INTERESTING.search(ctype) or req.resource_type in ("xhr", "fetch"):
                    try:
                        rec["body"] = scrub(resp.text())[:MAX_BODY]
                    except Exception as exc:
                        rec["body"] = f"<<unreadable: {exc}>>"
                records.append(rec)
            except Exception:
                pass

        page.on("response", on_response)

        print(f"[1/4] 打开 {D2_URL} ...")
        page.goto(D2_URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(3000)

        (OUT / "01_login_page.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(OUT / "01_login_page.png"), full_page=True)

        # 把登录表单的输入框结构单独抓出来（只抓结构，不抓值）
        forms = page.evaluate(
            """() => [...document.querySelectorAll('form')].map(f => ({
                    action: f.getAttribute('action'), method: f.getAttribute('method'),
                    id: f.id, name: f.getAttribute('name'),
                    fields: [...f.querySelectorAll('input,select,button')].map(i => ({
                        tag: i.tagName, type: i.getAttribute('type'), name: i.getAttribute('name'),
                        id: i.id, placeholder: i.getAttribute('placeholder'),
                        value: (i.getAttribute('type') || '').toLowerCase() === 'hidden' ? i.value : null
                    }))
               }))"""
        )
        (OUT / "02_login_forms.json").write_text(
            json.dumps(forms, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("[2/4] 登录页结构已导出。")

        print()
        print("=" * 70)
        print("请在弹出的浏览器窗口里手动登录，并进入 Task list 页面（能看到列表为止）。")
        print("如果列表默认只显示第 1 页，请顺手点一下第 2 页，让翻页接口也被记录。")
        print("完成后回到这个窗口，按回车继续。")
        print("=" * 70)
        input()

        page.wait_for_timeout(1500)
        (OUT / "03_after_login.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(OUT / "03_after_login.png"), full_page=False)

        # 所有 iframe 的 HTML（D2 有可能把列表放在 iframe 里）
        frames = []
        for i, fr in enumerate(page.frames):
            try:
                frames.append({"index": i, "name": fr.name, "url": fr.url})
                if fr != page.main_frame:
                    (OUT / f"04_frame_{i}.html").write_text(fr.content(), encoding="utf-8")
            except Exception:
                pass
        (OUT / "04_frames.json").write_text(
            json.dumps(frames, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # 列表的表头 + 前若干行文本，用来确认列顺序
        try:
            grid = page.evaluate(
                """() => {
                    const txt = el => (el.innerText || '').trim();
                    const heads = [...document.querySelectorAll('.x-grid3-hd-inner, th, [role=columnheader]')].map(txt).filter(Boolean);
                    const rows  = [...document.querySelectorAll('.x-grid3-row, tr[role=row], [role=row]')]
                                    .slice(0, 8)
                                    .map(r => [...r.querySelectorAll('.x-grid3-cell-inner, td, [role=gridcell]')].map(txt));
                    return {heads, rows};
                }"""
            )
        except Exception as exc:
            grid = {"error": str(exc)}
        (OUT / "05_grid_preview.json").write_text(
            json.dumps(grid, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("[3/4] 登录后页面与列表结构已导出。")

        # 顺手保存会话状态，正式脚本可复用（含 cookie，注意保密）
        try:
            ctx.storage_state(path=str(OUT / "06_storage_state.json"))
        except Exception:
            pass

        with (OUT / "requests.jsonl").open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[4/4] 共记录 {len(records)} 个请求 -> probe_out\\requests.jsonl")

        browser.close()

    print()
    print("完成。请把 probe_out 目录留在 D:\\codingSpace\\D2_agent 下，然后在对话里告诉我。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
