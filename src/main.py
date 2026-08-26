"""
M1 入口：取昨天收到的条目 -> 归一化 -> 控制台汇总（供跟 D2 界面对账）。
报告(HTML/xlsx)与发信在 M2/M3，需先定收件组/语言等业务口径。

用法：
    python src/main.py                # 昨天（周一自动覆盖周末）；默认用 config.ini 的 [d2] account
    python src/main.py --all          # 顺带打印整个 inbox 的规模
    python src/main.py --no-weekend   # 周一只算上周五那一天
    python src/main.py --draft        # 额外在 Outlook 生成草稿（只存草稿，绝不自动发送）
    python src/main.py --days=7       # 改看最近 7 天（补跑/测试；昨天没数据时好用）
    python src/main.py --date=today   # 指定某本地日期（YYYY-MM-DD 或 today）
    python src/main.py --dates        # 诊断：打印 inbox 里 date_sent 的日期分布
    python src/main.py --inbox=XXX    # 查别的队列（需登录身份对它有读权限）

换登录账号：运行时在「D2 登录名 [默认账号]」处直接输入别的账号即可；
或设环境变量 D2_USER / D2_PASSWORD（免交互，供计划任务用）。
默认查询的 inbox = 登录账号本人（读自己的队列一定有权限）。

只读、不发信、不改 D2 任何内容。
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import config
import fetch_content
import fetch_rest
import normalize
import report
import send_outlook


def summarize(items: list[normalize.Item]) -> None:
    total = len(items)
    parsed = [it for it in items if it.parsed_ok]
    unparsed = [it for it in items if not it.parsed_ok]

    by_country = Counter(it.country for it in parsed)
    by_doctype = Counter(it.doc_type for it in parsed)
    by_cat = Counter(c["code"] for it in parsed for c in it.categories if c.get("code"))

    print(f"\n条目总数：{total}    解析成功：{len(parsed)}    未解析(空/异常)：{len(unparsed)}")

    def dump(title, counter):
        print(f"\n  按{title}：")
        for k, v in counter.most_common():
            print(f"    {str(k):<28} {v}")

    dump("国家", by_country)
    dump("文书类型", by_doctype)
    dump("类别码", by_cat)

    print("\n  明细：")
    print(f"    {'Identifier':<15} {'类型':<13} {'国家':<5} {'状态':<10} 本地时间(日内瓦)")
    for it in sorted(parsed, key=lambda x: x.date_sent_utc or 0, reverse=True):
        loc = it.date_sent_local.strftime("%Y-%m-%d %H:%M") if it.date_sent_local else "?"
        print(f"    {it.identifier or '?':<15} {it.doc_type or '?':<13} "
              f"{it.country or '?':<5} {it.task_state or '?':<10} {loc}")
    if unparsed:
        print(f"\n  ⚠ {len(unparsed)} 条 task_subject 为空/异常（多为刚到达的 dormant 任务）：")
        for it in unparsed:
            loc = it.date_sent_local.strftime("%Y-%m-%d %H:%M") if it.date_sent_local else "?"
            print(f"    queue_id={it.queue_id} state={it.task_state} 本地时间={loc} raw={it.task_subject!r}")


def main(argv: list[str]) -> int:
    show_all = "--all" in argv
    settings = config.load()
    cover_weekend = ("--no-weekend" not in argv) and settings.cover_weekend_on_monday

    inbox_override = next((a.split("=", 1)[1] for a in argv if a.startswith("--inbox=")), None)

    if not settings.base_url:
        print("⚠ 未配置 D2 主机。请把 config.example.ini 复制为 config.ini 并填 [d2] host/repo/account。")
        return 1
    print("取数（只读 DQL over D2-REST）...")
    user, pwd = fetch_rest.get_credentials(default_account=settings.d2_account)
    client = fetch_rest.D2Client(user, pwd, base=settings.base_url, repo=settings.d2_repo)

    inbox = inbox_override or os.environ.get("D2_INBOX") or user
    print(f"登录身份：{user}    查询 inbox：{inbox}")

    rows = fetch_rest.fetch_inbox(client, account=inbox)
    items = [normalize.item_from_row(r) for r in rows]
    print(f"inbox 未删除条目共 {len(items)} 条（对账基准；界面右上角数字应与此接近）")

    if "--dates" in argv:   # 诊断：看 inbox 里 date_sent 的时间分布
        parsed = [it.date_sent_local for it in items if it.date_sent_utc]
        print(f"\n[dates] 有 date_sent {len(parsed)} 条，无/解析失败 {len(items) - len(parsed)} 条")
        if parsed:
            print(f"[dates] 最早 {min(parsed):%Y-%m-%d %H:%M}  最新 {max(parsed):%Y-%m-%d %H:%M}（日内瓦本地）")
            hist = Counter(d.strftime("%Y-%m-%d") for d in parsed)
            print("[dates] 按本地日期计数（最近 14 个有数据的日期）：")
            for day in sorted(hist)[-14:]:
                print(f"    {day}: {hist[day]}")
        return 0

    date_arg = next((a.split("=", 1)[1] for a in argv if a.startswith("--date=")), None)
    days_arg = next((a.split("=", 1)[1] for a in argv if a.startswith("--days=")), None)
    if date_arg:
        if date_arg == "today":
            now = datetime.now(normalize.LOCAL_TZ)
            y, m, d = now.year, now.month, now.day
        else:
            y, m, d = (int(x) for x in date_arg.split("-"))
        start_utc, end_utc = normalize.day_bounds_utc(y, m, d)
        label = f"{y:04d}-{m:02d}-{d:02d}"
    elif days_arg and days_arg.isdigit():
        start_utc, end_utc = normalize.last_n_days_bounds_utc(int(days_arg))
        label = f"最近 {int(days_arg)} 天"
    else:
        start_utc, end_utc = normalize.yesterday_bounds_utc(cover_weekend_on_monday=cover_weekend)
        span = round((end_utc - start_utc).total_seconds() / 86400)
        label = "昨天" + ("（含周末）" if span > 1 else "")
    s_loc = start_utc.astimezone(normalize.LOCAL_TZ).strftime("%Y-%m-%d")
    e_loc = (end_utc.astimezone(normalize.LOCAL_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    win = s_loc if s_loc == e_loc else f"{s_loc} ~ {e_loc}"
    print(f"窗口：{label}  本地 {win}  (UTC [{start_utc.isoformat()}, {end_utc.isoformat()}))")

    yday = [it for it in items if normalize.in_range(it, start_utc, end_utc)]
    print(f"\n========== {label}收到 ==========")
    summarize(yday)

    if show_all:
        print("\n========== 整个 inbox ==========")
        summarize(items)

    # ---- M2：生成报告（HTML + xlsx），只写本地 output/，不发送 ----
    meta = {
        "window_start_local": start_utc.astimezone(normalize.LOCAL_TZ),
        "window_end_local": end_utc.astimezone(normalize.LOCAL_TZ),
        "generated_at": datetime.now(normalize.LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z"),
        "repo": settings.d2_repo,
        "inbox": inbox,
        "title": settings.report_title,
    }
    want_draft = "--draft" in argv

    # ---- M2.5：--draft 且开启时，只读下载 D2 附件（docmail 虚拟文档的子文件）----
    per_item_att, att_paths = None, []
    if want_draft and settings.attach_documents:
        stamp = (meta["window_end_local"] - timedelta(days=1)).strftime("%Y%m%d")
        att_dir = os.path.join("output", "attachments", stamp)
        print(f"\n下载 D2 附件（只读，总上限 {settings.attach_max_total_mb:g}MB）...")
        per_item_att, att_paths, total = fetch_content.collect_for_items(
            client, yday, att_dir, int(settings.attach_max_total_mb * 1024 * 1024))
        omitted = sum(len(ia.omitted) for ia in per_item_att)
        errs = sum(1 for ia in per_item_att if ia.error)
        print(f"  已下载 {len(att_paths)} 个附件，合计 {total/1024/1024:.1f}MB"
              + (f"，{omitted} 个超限未附" if omitted else "")
              + (f"，{errs} 条解析出错" if errs else ""))

    # ---- M2：生成报告（HTML + xlsx）----
    html_path, xlsx_path = report.generate(yday, meta, outdir="output", attachments=per_item_att)
    print("\n========== 报告已生成（未发送）==========")
    print(f"  HTML 正文预览：{html_path}")
    print(f"  xlsx 明细附件：{xlsx_path}")

    # ---- M3：Outlook 草稿（仅 --draft；只存草稿，绝不自动发送）----
    if want_draft:
        if not settings.recipients:
            print("\n⚠ config.ini 未配置收件人，跳过草稿。")
            return 0
        subject = f"{settings.subject_prefix} {report.window_label(meta)} ({len(yday)} items)"
        body = Path(html_path).read_text(encoding="utf-8")   # 邮件正文=已生成的 HTML
        all_att = [xlsx_path] + att_paths
        print("\n生成 Outlook 草稿（只存草稿，不发送）...")
        res = send_outlook.make_draft(
            subject, body, settings.recipients, attachments=all_att,
            send_from=settings.send_from, show=True, send=False)
        print(f"  模式：{res['mode']}    收件人：{', '.join(settings.recipients)}")
        print(f"  附件：xlsx 明细 + {len(att_paths)} 个 D2 文档")
        print(f"  {res['detail']}")
        if res.get("note"):
            print(f"  提示：{res['note']}")
    else:
        print(f"  收件人（config.ini）：{', '.join(settings.recipients) or '未配置'}")
        print("  （加 --draft 生成 Outlook 草稿并附带 D2 文档；本项目不会自动发送）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
