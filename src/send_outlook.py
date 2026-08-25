r"""
M3 发信层：把 M2 的 HTML 正文 + xlsx 附件做成邮件。

安全原则：**默认只生成草稿，绝不自动发送。** `.Send()` 仅在调用方显式传 send=True 时才会执行；
本项目的入口(main.py --draft)永远只调草稿模式。

两条路（自动选择）：
  1) 首选 pywin32(win32com) → 在你本机 Outlook 的『草稿箱』里生成可编辑草稿，
     你亲眼核对收件人/正文/附件后再决定是否发送。
  2) 没装 pywin32 或起不了 Outlook → 用标准库 email 生成 .eml 落到 output/，
     双击即可在 Outlook 里打开另存/编辑。

依赖：pywin32（仅路 1 需要，`pip install pywin32`）；路 2 纯标准库。
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

OL_MAIL_ITEM = 0  # olMailItem


def _abspaths(attachments: list[str]) -> list[str]:
    return [str(Path(a).resolve()) for a in (attachments or []) if a]


def make_draft(subject: str, html_body: str, to: list[str],
               attachments: list[str] | None = None, send_from: str = "",
               show: bool = True, send: bool = False, outdir: str = "output") -> dict:
    """
    生成草稿（默认）。返回 {"mode": ..., "detail": ...}。
    send=True 才会真正发送——入口不会传，仅留给将来显式授权时用。
    """
    attachments = _abspaths(attachments)
    try:
        return _via_outlook(subject, html_body, to, attachments, send_from, show, send)
    except Exception as exc:  # noqa: BLE001  # 没装 pywin32 / 起不了 Outlook 都走兜底
        eml = write_eml(subject, html_body, to, attachments, outdir, send_from)
        return {"mode": "eml", "detail": eml,
                "note": f"未能驱动 Outlook（{type(exc).__name__}: {exc}）；已生成 .eml，双击可在 Outlook 打开。"}


def _via_outlook(subject, html_body, to, attachments, send_from, show, send) -> dict:
    import win32com.client  # 需要 pip install pywin32

    outlook = win32com.client.Dispatch("Outlook.Application")
    mail = outlook.CreateItem(OL_MAIL_ITEM)
    mail.To = "; ".join(to)
    mail.Subject = subject
    mail.HTMLBody = html_body
    if send_from:
        mail.SentOnBehalfOfName = send_from  # 需要你的 Outlook 对该邮箱有 send-on-behalf 权限
    for path in attachments:
        mail.Attachments.Add(Source=path)

    if send:
        mail.Send()
        return {"mode": "outlook_sent", "detail": f"已发送给 {mail.To}"}

    mail.Save()          # 存入『草稿箱』
    if show:
        mail.Display(False)  # 非模态打开，供你核对
    return {"mode": "outlook_draft",
            "detail": "已存入 Outlook 草稿箱（并打开供核对）" if show else "已存入 Outlook 草稿箱"}


def _ctype(name: str) -> tuple[str, str]:
    low = name.lower()
    if low.endswith(".xlsx"):
        return "application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    guessed, _ = mimetypes.guess_type(name)
    if guessed and "/" in guessed:
        main, sub = guessed.split("/", 1)
        return main, sub
    return "application", "octet-stream"


def write_eml(subject: str, html_body: str, to: list[str],
              attachments: list[str] | None = None, outdir: str = "output",
              send_from: str = "") -> str:
    """标准库兜底：写一封 .eml（HTML 正文 + 附件），可在 Outlook 打开。返回路径。"""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = ", ".join(to)
    if send_from:
        msg["From"] = send_from
    msg.set_content("This report is best viewed in an HTML-capable mail client. "
                    "See the HTML part and the attached spreadsheet.")
    msg.add_alternative(html_body, subtype="html")
    for path in _abspaths(attachments):
        main, sub = _ctype(path)
        msg.add_attachment(Path(path).read_bytes(), maintype=main, subtype=sub,
                           filename=Path(path).name)

    Path(outdir).mkdir(parents=True, exist_ok=True)
    stem = Path(attachments[0]).stem if attachments else "D2_draft"
    out = Path(outdir) / f"{stem}.eml"
    out.write_bytes(bytes(msg))
    return str(out)
