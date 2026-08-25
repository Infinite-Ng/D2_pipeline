"""
归一化层：把 dmi_queue_item 的原始行 + task_subject 解析成结构化 Item。
纯标准库、无副作用、可离线单测（不碰网络、不碰凭据）。

task_subject 的实际格式（来自 M0 探测，13 个真实样本）：

    I-2099-000001 Handle incoming E-COMM 1/2/2099, 7:47:57 AM 09A ADVANCE PUBLICATION, 09C SPACE SYSTEM COORDINATION  ZZZ (CSS)
    └ Identifier ┘└──── action ────┘     └──── 本地时间 ────┘  └────────────── categories ──────────────┘             └国家┘└单元┘
    （合成示例，格式与真实一致，不含真实业务数据）

要点：
* date_sent 是 UTC（JSON 里带 +00:00）；task_subject 里的时间是日内瓦本地时（UTC+1/+2）。
  "昨天" 的口径按**本地时区**算边界，再换算成 UTC 去筛 date_sent。
* 偶发的 dormant 任务 task_subject 可能为空（到达后几分钟内的瞬时状态）；
  这类 parsed_ok=False，保留原始行，交给上层决定兜底或标记。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Zurich")  # ITU 总部
except Exception:  # noqa: BLE001  # 极老环境兜底
    LOCAL_TZ = timezone(timedelta(hours=2))

_IDENT = re.compile(r"(I-\d{4}-\d{6})")
_DATETIME = re.compile(r"(\d{1,2}/\d{1,2}/\d{4}),\s*(\d{1,2}:\d{2}:\d{2}\s*[AP]M)")
_TAIL = re.compile(r"([A-Z]{2,4})\s*\(([^)]+)\)\s*$")   # 结尾的 "THA (CSS)"
_DOCTYPE = re.compile(r"\b(E-[A-Z]+)\b")                 # E-COMM / E-SUBMISSION
_CAT = re.compile(r"^(\d{2}[A-Z])\s+(.*)$")             # "09C SPACE SYSTEM COORDINATION"


def parse_subject(subject: str | None) -> dict:
    """把 task_subject 解析成字段字典。解析不出关键字段时 parsed_ok=False。"""
    out = {
        "identifier": None, "action": None, "doc_type": None,
        "doc_datetime_local": None, "categories": [], "country": None,
        "unit": None, "raw": subject, "parsed_ok": False,
    }
    # 真实数据里 token 之间混用了不间断空格(\xa0)，先归一化再解析，输出才干净
    s = re.sub(r"\s+", " ", (subject or "").replace("\xa0", " ")).strip()
    if not s:
        return out

    m_id = _IDENT.search(s)
    if m_id:
        out["identifier"] = m_id.group(1)

    m_dt = _DATETIME.search(s)
    start = m_id.end() if m_id else 0
    if m_dt:
        out["action"] = s[start:m_dt.start()].strip() or None
        out["doc_datetime_local"] = f"{m_dt.group(1)} {m_dt.group(2)}".replace("  ", " ")
        after = s[m_dt.end():].strip()
    else:
        out["action"] = s[start:].strip() or None
        after = ""

    if out["action"]:
        m_ty = _DOCTYPE.search(out["action"])
        if m_ty:
            out["doc_type"] = m_ty.group(1)

    if after:
        m_tail = _TAIL.search(after)
        if m_tail:
            out["country"] = m_tail.group(1)
            out["unit"] = m_tail.group(2)
            cats_str = after[:m_tail.start()].strip()
        else:
            cats_str = after
        for part in (p.strip() for p in cats_str.split(",") if p.strip()):
            m_c = _CAT.match(part)
            if m_c:
                out["categories"].append({"code": m_c.group(1), "name": m_c.group(2).strip()})
            else:
                out["categories"].append({"code": None, "name": part})

    out["parsed_ok"] = bool(out["identifier"] and out["country"])
    return out


def _parse_date_sent(value: str | None) -> datetime | None:
    """'2026-08-21T05:49:51.000+00:00' -> aware datetime(UTC)。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class Item:
    queue_id: str | None = None
    item_id: str | None = None
    router_id: str | None = None
    date_sent_utc: datetime | None = None
    sent_by: str | None = None
    supervisor: str | None = None
    priority: int | None = None
    task_state: str | None = None
    read_flag: int | None = None
    # 从 task_subject 解析出来的：
    identifier: str | None = None
    doc_type: str | None = None
    action: str | None = None
    doc_datetime_local: str | None = None
    categories: list = field(default_factory=list)
    country: str | None = None
    unit: str | None = None
    task_subject: str | None = None
    parsed_ok: bool = False

    @property
    def date_sent_local(self) -> datetime | None:
        return self.date_sent_utc.astimezone(LOCAL_TZ) if self.date_sent_utc else None


def item_from_row(props: dict) -> Item:
    """dmi_queue_item 的 properties -> Item。"""
    p = parse_subject(props.get("task_subject"))
    return Item(
        queue_id=props.get("r_object_id"),
        item_id=props.get("item_id"),
        router_id=props.get("router_id"),
        date_sent_utc=_parse_date_sent(props.get("date_sent")),
        sent_by=props.get("sent_by"),
        supervisor=props.get("supervisor_name"),
        priority=props.get("priority"),
        task_state=props.get("task_state"),
        read_flag=props.get("read_flag"),
        identifier=p["identifier"], doc_type=p["doc_type"], action=p["action"],
        doc_datetime_local=p["doc_datetime_local"], categories=p["categories"],
        country=p["country"], unit=p["unit"],
        task_subject=props.get("task_subject"), parsed_ok=p["parsed_ok"],
    )


def yesterday_bounds_utc(now_local: datetime | None = None,
                         cover_weekend_on_monday: bool = True) -> tuple[datetime, datetime]:
    """
    返回 [start, end) 的 UTC 边界，用于筛 date_sent。
    "昨天" 按本地时区（Europe/Zurich）算：
      * 平常：昨天 00:00 ~ 今天 00:00
      * 周一且 cover_weekend_on_monday：上周五 00:00 ~ 今天 00:00（覆盖周末）
    now_local 仅供测试注入；生产不传，用当前时间。
    """
    if now_local is None:
        now_local = datetime.now(LOCAL_TZ)
    if now_local.tzinfo is None:
        now_local = now_local.replace(tzinfo=LOCAL_TZ)

    today0 = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = today0
    days_back = 1
    if cover_weekend_on_monday and now_local.weekday() == 0:  # Monday=0
        days_back = 3
    start_local = today0 - timedelta(days=days_back)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def in_range(item: Item, start_utc: datetime, end_utc: datetime) -> bool:
    return bool(item.date_sent_utc and start_utc <= item.date_sent_utc < end_utc)
