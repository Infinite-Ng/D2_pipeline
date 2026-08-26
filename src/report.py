"""
报告层：把归一化后的 Item 列表渲染成
  1) 邮件 HTML 正文（English，内联 CSS，邮件客户端友好）
  2) xlsx 明细附件（纯标准库 xlsx_min，无需 openpyxl）

只做渲染，不发信、不碰网络。语言固定 English（业务确认）。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import xlsx_min
from normalize import Item

_MIN = datetime.min.replace(tzinfo=timezone.utc)  # 排序时给缺失时间兜底

ITEM_COLUMNS = [
    "Identifier", "Type", "Country", "Unit", "Categories",
    "Received (Geneva)", "Date sent (UTC)", "Sender", "Supervisor",
    "Priority", "State", "Queue ID", "Task subject (raw)",
]


def _fmt_local(it: Item) -> str:
    d = it.date_sent_local
    return d.strftime("%Y-%m-%d %H:%M") if d else ""


def _fmt_utc(it: Item) -> str:
    return it.date_sent_utc.strftime("%Y-%m-%d %H:%M:%S") if it.date_sent_utc else ""


def _cats(it: Item) -> str:
    return "; ".join(
        (f'{c["code"]} {c["name"]}'.strip() if c.get("code") else c.get("name", ""))
        for c in it.categories
    )


def summarize(items: list[Item]) -> dict:
    parsed = [it for it in items if it.parsed_ok]
    return {
        "total": len(items),
        "parsed": len(parsed),
        "unparsed": len(items) - len(parsed),
        "by_country": Counter(it.country for it in parsed),
        "by_doctype": Counter(it.doc_type for it in parsed),
        "by_category": Counter(
            f'{c["code"]} {c["name"]}'.strip()
            for it in parsed for c in it.categories if c.get("code")
        ),
    }


def window_label(meta: dict) -> str:
    start = meta["window_start_local"]
    end = meta["window_end_local"]
    last_day = (end - timedelta(days=1)).date()
    first_day = start.date()
    if first_day == last_day:
        return first_day.strftime("%A, %d %b %Y")
    return f'{first_day.strftime("%a %d %b")} – {last_day.strftime("%a %d %b %Y")}'


# --------------------------------------------------------------- HTML
_H = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))  # noqa: E731


def _human_size(n: int) -> str:
    f = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} GB"


def _attachments_html(attachments) -> str:
    """attachments: list[ItemAttachments]（鸭子类型：.identifier/.attached/.omitted/.error）。"""
    if not attachments:
        return ""
    n_files = sum(len(ia.attached) for ia in attachments)
    n_bytes = sum(a.size for ia in attachments for a in ia.attached)
    omitted = [(ia.identifier, a) for ia in attachments for a in ia.omitted]
    if not n_files and not omitted:
        return ""
    rows = []
    for ia in attachments:
        if not ia.attached:
            continue
        files = ", ".join(_H(a.filename) for a in ia.attached)
        rows.append(f'<tr><td style="padding:3px 12px 3px 0;white-space:nowrap">'
                    f'{_H(ia.identifier or "—")}</td><td style="padding:3px 0">{files}</td></tr>')
    body = (f'<table style="border-collapse:collapse;font-size:12px">{"".join(rows)}</table>'
            if rows else '<div style="font-size:12px;color:#6b7280">（无）</div>')
    omit = ""
    if omitted:
        items = "; ".join(f'{_H(idn or "—")}: {_H(a.filename)} ({_human_size(a.size)})'
                          for idn, a in omitted)
        omit = (f'<p style="color:#b45309;font-size:12px;margin:8px 0 0">'
                f'⚠ {len(omitted)} file(s) not attached (over the size cap) — download from D2: {items}</p>')
    return (f'<h3 style="margin:18px 0 6px">Attachments '
            f'<span style="font-weight:400;color:#6b7280;font-size:13px">'
            f'({n_files} file(s), {_human_size(n_bytes)})</span></h3>{body}{omit}')


def build_html(items: list[Item], meta: dict, attachments=None) -> str:
    s = summarize(items)
    win = window_label(meta)
    att_html = _attachments_html(attachments)

    def kv_table(counter: Counter, head: str) -> str:
        if not counter:
            return ""
        rows = "".join(
            f'<tr><td style="padding:3px 12px 3px 0">{_H(k)}</td>'
            f'<td style="padding:3px 0;text-align:right">{v}</td></tr>'
            for k, v in counter.most_common()
        )
        return (f'<div style="display:inline-block;vertical-align:top;margin:0 32px 12px 0">'
                f'<div style="font-weight:600;margin-bottom:4px">{head}</div>'
                f'<table style="border-collapse:collapse;font-size:13px">{rows}</table></div>')

    detail_head = "".join(
        f'<th style="text-align:left;padding:6px 10px;border-bottom:2px solid #ccc;'
        f'white-space:nowrap">{c}</th>'
        for c in ["Identifier", "Type", "Country", "Categories", "Received (Geneva)",
                  "Sender", "Priority", "State"]
    )
    detail_rows = []
    for i, it in enumerate(sorted(items, key=lambda x: x.date_sent_utc or _MIN, reverse=True)):
        bg = "#fafafa" if i % 2 else "#ffffff"
        cells = [
            it.identifier or "—", it.doc_type or "—", it.country or "—", _cats(it) or "—",
            _fmt_local(it), it.sent_by or "—",
            str(it.priority) if it.priority is not None else "—", it.task_state or "—",
        ]
        tds = "".join(f'<td style="padding:5px 10px;border-bottom:1px solid #eee;'
                      f'white-space:nowrap">{_H(c)}</td>' for c in cells)
        detail_rows.append(f'<tr style="background:{bg}">{tds}</tr>')

    warn = ""
    if s["unparsed"]:
        warn = (f'<p style="color:#b45309;font-size:12px;margin:10px 0 0">'
                f'⚠ {s["unparsed"]} item(s) had an empty/unparsable subject '
                f'(typically just-arrived “dormant” tasks) and are omitted from the breakdown.</p>')

    return f"""\
<div style="font-family:Segoe UI,Arial,sans-serif;color:#1f2937;max-width:960px">
  <h2 style="margin:0 0 2px">{_H(meta.get("title", "Documentum D2 Daily Intake"))}</h2>
  <div style="color:#6b7280;font-size:13px;margin-bottom:16px">
    Items received: <b>{win}</b> &nbsp;·&nbsp; generated {_H(meta.get("generated_at",""))}
    &nbsp;·&nbsp; source: D2 <code>{_H(meta.get("repo",""))}</code> / inbox
    <code>{_H(meta.get("inbox",""))}</code> (read-only)
  </div>

  <div style="font-size:22px;font-weight:700;margin-bottom:10px">
    {s["total"]} item(s) received
  </div>

  {kv_table(s["by_country"], "By country")}
  {kv_table(s["by_doctype"], "By document type")}
  {kv_table(s["by_category"], "By category")}

  <h3 style="margin:18px 0 6px">Detail</h3>
  <table style="border-collapse:collapse;font-size:13px;width:100%">
    <thead><tr>{detail_head}</tr></thead>
    <tbody>{"".join(detail_rows) or '<tr><td style="padding:8px">No items.</td></tr>'}</tbody>
  </table>
  {att_html}
  {warn}
  <p style="color:#9ca3af;font-size:11px;margin-top:18px">
    Full detail (all columns) is in the attached spreadsheet. Automated report — do not reply.
  </p>
</div>"""


# --------------------------------------------------------------- xlsx
def build_xlsx_sheets(items: list[Item], meta: dict) -> list:
    s = summarize(items)
    item_rows = [ITEM_COLUMNS]
    for it in sorted(items, key=lambda x: x.date_sent_utc or _MIN, reverse=True):
        item_rows.append([
            it.identifier, it.doc_type, it.country, it.unit, _cats(it),
            _fmt_local(it), _fmt_utc(it), it.sent_by, it.supervisor,
            it.priority, it.task_state, it.queue_id, it.task_subject,
        ])

    summary_rows = [["Metric", "Value"],
                    ["Window (Geneva local)", window_label(meta)],
                    ["Generated", meta.get("generated_at", "")],
                    ["Inbox", meta.get("inbox", "")],
                    ["Total received", s["total"]],
                    ["Parsed", s["parsed"]],
                    ["Unparsed", s["unparsed"]],
                    [], ["By country", ""]]
    summary_rows += [[k, v] for k, v in s["by_country"].most_common()]
    summary_rows += [[], ["By document type", ""]]
    summary_rows += [[k, v] for k, v in s["by_doctype"].most_common()]
    summary_rows += [[], ["By category", ""]]
    summary_rows += [[k, v] for k, v in s["by_category"].most_common()]

    return [("Items", item_rows), ("Summary", summary_rows)]


# --------------------------------------------------------------- entry
def generate(items: list[Item], meta: dict, outdir: str = "output",
             attachments=None) -> tuple[str, str]:
    Path(outdir).mkdir(parents=True, exist_ok=True)
    stamp = (meta["window_end_local"] - timedelta(days=1)).strftime("%Y%m%d")
    base = Path(outdir) / f"D2_daily_{stamp}"
    html_path = f"{base}.html"
    xlsx_path = f"{base}.xlsx"
    Path(html_path).write_text(build_html(items, meta, attachments), encoding="utf-8")
    xlsx_min.write_workbook(xlsx_path, build_xlsx_sheets(items, meta))
    return html_path, xlsx_path
