r"""
D2 REST 探测（M0c，修正版 —— 定位业务对象链，只读 GET，不发邮件）
========================================================================
第一次带凭据探测（probe_api.py）已确认：DQL 可用、账号可登（见 config.ini）、inbox 有数据、
dmi_queue_item.task_subject 里带 Identifier/国家/类别。但发现两点需要收尾：

  1. dmi_queue_item.item_id 其实是 dmi_workitem(4a...)，不是业务文档；
     业务文档（带 I-识别号、国家等干净属性）挂在 workflow 的 package 上。
  2. 最新的 dormant 任务 task_subject 为空 —— 这类要靠业务对象兜底。

本脚本沿正确的链路走一遍（全部只读 GET）：
     dmi_queue_item.router_id ─▶ dm_workflow
                                    └─ dmi_package.r_component_id ─▶ 业务文档
  A. 重新取 inbox 前 8 条（含 router_id / task_subject / task_state）
  B. 对每条：查 dmi_package(r_workflow_id=router_id) 拿 r_component_id + 包类型
  C. 解析 component 的真实 r_object_type，再 select * 把它全部属性 dump 出来
     —— 这一步定稿 Identifier / Sending / Receiving / 国家 到底对应哪些 attribute
  D. 顺带确认 item_id 的真实类型（应为 dmi_workitem）

结果写到 probe_out\api2_*.json（含业务数据、无密码，已被 .gitignore 挡住）。
纯标准库，直接： python probe_api2.py
"""
from __future__ import annotations

import base64
import getpass
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from d2conf import load_d2

_HOST, REPO, _ACCOUNT = load_d2()  # 内网连接从 config.ini 读，不硬编码
BASE = f"https://{_HOST}/D2-REST"
OUT = Path(__file__).parent / "probe_out"
TIMEOUT = 30

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE
_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ctx))
_auth = ""


def save(name: str, obj) -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / f"api2_{name}.json").write_text(
        json.dumps(obj, indent=2, ensure_ascii=False)[:800_000], encoding="utf-8"
    )


def get(url: str, params: dict | None = None) -> tuple[int, str]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET", headers={
        "Accept": "application/json", "User-Agent": "D2-probe/3", "Authorization": _auth})
    try:
        with _opener.open(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return e.code, ""
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def as_json(t: str):
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        return None


def entries_of(data) -> list:
    if not isinstance(data, dict):
        return []
    for k in ("entries", "results", "items"):
        if isinstance(data.get(k), list):
            return data[k]
    return []


def props_of(entry: dict) -> dict:
    if not isinstance(entry, dict):
        return {}
    for path in (("content", "properties"), ("properties",), ("object", "properties")):
        cur = entry
        ok = True
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None:
                ok = False
                break
        if ok and isinstance(cur, dict):
            return cur
    return {}


def dql(query: str, page_size: int = 10) -> dict:
    url = f"{BASE}/repositories/{urllib.parse.quote(REPO)}"
    st, text = get(url, {"dql": query, "items-per-page": page_size, "page": 1})
    data = as_json(text)
    ok = st == 200 and data is not None
    return {"ok": ok, "status": st, "query": query,
            "raw": None if ok else text[:20_000],
            "rows": [props_of(e) for e in entries_of(data)] if ok else []}


def esc(s: str) -> str:
    return (s or "").replace("'", "''")


def main() -> int:
    global _auth
    OUT.mkdir(exist_ok=True)
    print(f"目标 {BASE} repo={REPO}\n")
    user = input(f"    D2 登录名{f' [{_ACCOUNT}]' if _ACCOUNT else ''}: ").strip() or _ACCOUNT
    pwd = getpass.getpass("    D2 密码（不保存、不外传、我看不到）: ")
    _auth = "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()

    # A. 重新取 inbox 前 8 条
    print("[A] 取 inbox 前 8 条")
    inbox = dql("select r_object_id, item_id, router_id, task_subject, task_state, "
                "date_sent, sent_by from dmi_queue_item "
                f"where name = '{esc(user)}' and delete_flag = false "
                "order by date_sent desc", page_size=8)
    save("A_inbox", inbox)
    if not inbox["ok"]:
        print(f"    失败 status={inbox['status']}，见 api2_A_inbox.json")
        return 1
    rows = inbox["rows"]
    print(f"    取到 {len(rows)} 条")

    routers = [r.get("router_id") for r in rows if r.get("router_id")]

    # B. 每个 workflow 的 package -> r_component_id（用 select * 避免猜属性名）
    print("[B] 查每个 workflow 的 dmi_package -> 业务文档 id")
    pkg_all = []
    comp_ids: list[str] = []
    for rid in routers[:5]:
        pk = dql(f"select * from dmi_package where r_workflow_id = '{esc(rid)}'", page_size=20)
        pkg_all.append({"router_id": rid, "result": pk})
        for r in pk["rows"]:
            cid = r.get("r_component_id")
            vals = cid if isinstance(cid, list) else [cid]  # r_component_id 是 repeating
            for v in vals:
                if v and v not in comp_ids:
                    comp_ids.append(v)
    save("B_packages", pkg_all)
    print(f"    收集到 {len(comp_ids)} 个业务文档 id")

    # C. 业务文档真实类型 + 全属性
    if comp_ids:
        idlist = ",".join(f"'{esc(x)}'" for x in comp_ids)
        types = dql("select r_object_id, r_object_type, object_name, title, "
                    "r_creation_date, r_modify_date "
                    f"from dm_sysobject where r_object_id in ({idlist})", page_size=50)
        save("C_component_types", types)
        typ_set = sorted({r.get("r_object_type") for r in types["rows"] if r.get("r_object_type")})
        print(f"[C] 业务文档类型: {typ_set}")

        # 对每种类型，挑一个样本 dump 全部属性（定字段映射）
        dumps = []
        seen_types = set()
        for r in types["rows"]:
            typ = r.get("r_object_type")
            cid = r.get("r_object_id")
            if not typ or typ in seen_types:
                continue
            seen_types.add(typ)
            full = dql(f"select * from {typ} where r_object_id = '{esc(cid)}'", page_size=1)
            dumps.append({"r_object_type": typ, "sample_id": cid, "result": full})
            if full["ok"] and full["rows"]:
                keys = sorted(full["rows"][0].keys())
                print(f"    [{typ}] 属性 {len(keys)} 个: {', '.join(keys)}")
        save("C_component_full", dumps)

    print("\n完成。probe_out\\api2_*.json 已生成（含业务数据、无密码，已被 .gitignore 挡住）。")
    print("把这些文件留在文件夹里告我一声，我据此定稿字段映射并写 M1 取数模块。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
