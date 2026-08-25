"""
normalize 的离线单测（合成样本，不含真实业务数据）。
可直接跑： python tests/test_normalize.py     也兼容 pytest。
样本格式仿真 M0 探测所见（含不间断空格 \xa0、AM/PM、多类别、空 subject）。
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import normalize  # noqa: E402

# 用 \xa0 复现真实分隔符；标识号/国家为合成值
S_MULTI = "I-2099-000001\xa0Handle\xa0incoming E-COMM\xa01/2/2099, 7:47:57 AM 09A ADVANCE PUBLICATION, 09C SPACE SYSTEM COORDINATION\xa0\xa0ZZZ (CSS)"
S_SINGLE = "I-2099-000002 Handle incoming E-SUBMISSION 12/31/2099, 4:20:01 PM 09C SPACE SYSTEM COORDINATION  YYY (CSS)"


def test_multi_category():
    r = normalize.parse_subject(S_MULTI)
    assert r["parsed_ok"] is True
    assert r["identifier"] == "I-2099-000001"
    assert r["doc_type"] == "E-COMM"
    assert r["country"] == "ZZZ"
    assert r["unit"] == "CSS"
    assert r["action"] == "Handle incoming E-COMM"          # \xa0 已归一化
    assert r["doc_datetime_local"] == "1/2/2099 7:47:57 AM"
    assert [c["code"] for c in r["categories"]] == ["09A", "09C"]
    assert r["categories"][1]["name"] == "SPACE SYSTEM COORDINATION"


def test_single_category_pm():
    r = normalize.parse_subject(S_SINGLE)
    assert r["parsed_ok"] is True
    assert r["doc_type"] == "E-SUBMISSION"
    assert r["country"] == "YYY"
    assert len(r["categories"]) == 1 and r["categories"][0]["code"] == "09C"


def test_empty_subject():
    r = normalize.parse_subject("")
    assert r["parsed_ok"] is False and r["identifier"] is None


def test_date_sent_utc_and_local():
    it = normalize.item_from_row({
        "r_object_id": "1b0001", "task_subject": S_MULTI,
        "date_sent": "2026-08-21T05:49:51.000+00:00", "task_state": "acquired"})
    assert it.date_sent_utc.hour == 5
    assert it.date_sent_local.hour == 7        # 日内瓦夏令时 UTC+2
    assert it.identifier == "I-2099-000001"


def test_yesterday_regular_tuesday():
    tue = datetime(2026, 8, 25, 9, 0, tzinfo=normalize.LOCAL_TZ)
    start, end = normalize.yesterday_bounds_utc(tue, cover_weekend_on_monday=True)
    # 昨天=周一：本地 8/24 00:00 ~ 8/25 00:00 -> UTC 8/23 22:00 ~ 8/24 22:00
    assert start.isoformat() == "2026-08-23T22:00:00+00:00"
    assert end.isoformat() == "2026-08-24T22:00:00+00:00"


def test_yesterday_monday_covers_weekend():
    mon = datetime(2026, 8, 24, 9, 0, tzinfo=normalize.LOCAL_TZ)
    start, end = normalize.yesterday_bounds_utc(mon, cover_weekend_on_monday=True)
    # 覆盖 周五~周日：本地 8/21 00:00 ~ 8/24 00:00
    assert start.isoformat() == "2026-08-20T22:00:00+00:00"
    assert end.isoformat() == "2026-08-23T22:00:00+00:00"


def test_in_range():
    it = normalize.item_from_row({"task_subject": S_MULTI,
                                  "date_sent": "2026-08-24T10:00:00+00:00"})
    tue = datetime(2026, 8, 25, 9, 0, tzinfo=normalize.LOCAL_TZ)
    start, end = normalize.yesterday_bounds_utc(tue)
    assert normalize.in_range(it, start, end) is True


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    raise SystemExit(0 if passed == len(fns) else 1)
