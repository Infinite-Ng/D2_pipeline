r"""
D2 REST 『带凭据』探测脚本（M0 第二步，只读 GET，不发任何邮件）
==================================================================
前提已由 probe_recon.py（无凭据侦察）确认：
  * REST base = https://<host>/D2-REST （Documentum D2 REST Services；host 见 config.ini）
  * repository = <repo>                 （config.ini 的 [d2] repo）
  * auth_mode  = basic                  （用你的 D2 账号 Basic Auth 直连即可）

本脚本在此基础上，用你的账号做 5 件事，全部是只读 GET：
  1. 认证自检          GET /repositories/<repo>         -> 确认账号能登进去
  2. 查你的 dm_user    DQL                              -> 拿到库里的 user_name
  3. 查你的 inbox      DQL: select * from dmi_queue_item -> Task list 原始字段
  4. inbox 计数        DQL: count(*)                    -> 跟界面上的条数对账
  5. 展开第 1 条任务挂的对象，dump 全部属性             -> 找出 Identifier /
     Sending / Receiving 到底对应哪个 attribute（字段映射靠这一步定稿）

* 纯 Python 标准库，无需 pip 安装、无需 venv —— 直接 `python probe_api.py`。
* 密码用 getpass 当场读入：不落盘、不写日志、不进对话、我这边看不到。
* 每个请求的**原始响应**都会存进 probe_out\api_*.json，方便万一解析不准时回看。
  （probe_out\ 已被 .gitignore 挡住，绝不会上传到 public 仓库。）

用法：
    python probe_api.py
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

# ---- 内网连接从 config.ini 读，不硬编码进公开代码 ----
_HOST, REPO, _ACCOUNT = load_d2()
BASE = f"https://{_HOST}/D2-REST"

OUT = Path(__file__).parent / "probe_out"
TIMEOUT = 30

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE  # 内网自签证书
_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ctx))

_auth_header = ""  # main() 里填入


def save(name: str, obj) -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / f"api_{name}.json").write_text(
        json.dumps(obj, indent=2, ensure_ascii=False)[:800_000], encoding="utf-8"
    )


def get(url: str, params: dict | None = None) -> tuple[int, str]:
    """只读 GET，返回 (status, text)。异常一律转成 status=-1。"""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": "D2-agent-probe/2",
            "Authorization": _auth_header,
        },
    )
    try:
        with _opener.open(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def as_json(text: str):
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


def entries_of(data) -> list:
    if not isinstance(data, dict):
        return []
    for k in ("entries", "results", "items"):
        v = data.get(k)
        if isinstance(v, list):
            return v
    return []


def flatten_props(entry: dict) -> dict:
    """从一个 REST entry 里挖出 properties 字典（不同层级都试）。"""
    if not isinstance(entry, dict):
        return {}
    for path in (("properties",), ("content", "properties"),
                 ("object", "properties"), ("content", "object", "properties")):
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


def dql(query: str, page_size: int = 5) -> dict:
    """Documentum/D2 REST 的 DQL 端点：GET {base}/repositories/{repo}?dql=..."""
    params = {"dql": query, "items-per-page": page_size, "page": 1}
    url = f"{BASE}/repositories/{urllib.parse.quote(REPO)}"
    status, text = get(url, params)
    data = as_json(text)
    ok = status == 200 and data is not None
    return {"ok": ok, "url": url, "query": query, "status": status,
            "raw": text[:100_000] if not ok else None, "data": data}


def main() -> int:
    global _auth_header
    OUT.mkdir(exist_ok=True)

    print(f"目标: {BASE}  repo={REPO}")
    print("（由 probe_recon.py 确认: D2 REST 20.4, auth_mode=basic）\n")

    # 重新确认服务还在（匿名）
    st, _ = get(f"{BASE}/product-info")
    print(f"[0] product-info -> {st}  {'OK' if st == 200 else '注意：服务可能变化'}")

    user = input(f"    D2 登录名{f' [{_ACCOUNT}]' if _ACCOUNT else ''}: ").strip() or _ACCOUNT
    pwd = getpass.getpass("    D2 密码（不保存、不外传、我看不到）: ")
    _auth_header = "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()

    # ---- 1. 认证自检 ----
    print(f"\n[1] 认证自检: GET /repositories/{REPO}")
    st, text = get(f"{BASE}/repositories/{REPO}")
    save("01_auth_check", {"status": st, "body": as_json(text) or text[:5000]})
    if st == 401:
        print("    401 未通过认证 —— 账号或密码不对，或该账号无 REST 权限。停。")
        return 1
    if st != 200:
        print(f"    返回 {st}（预期 200）。详见 probe_out\\api_01_auth_check.json。继续尝试 DQL...")
    else:
        print("    认证通过 ✓")

    # ---- 2. dm_user ----
    print("[2] 查 dm_user，确认库里的 user_name")
    r = dql(f"select user_name, user_login_name, user_os_name, user_address, r_is_group "
            f"from dm_user "
            f"where user_login_name = '{user}' or user_os_name = '{user}' "
            f"or user_name = '{user}'")
    save("02_dm_user", r)
    if not r["ok"]:
        print(f"    DQL 不可用/失败: status={r['status']}")
        print(f"    原始响应见 probe_out\\api_02_dm_user.json —— 很可能是该账号未开放 DQL 权限。")
        print("    若此步失败，请把 api_01/api_02 两个文件发我，我改用对象导航方式。")
        return 1
    print("    DQL 可用 ✓")
    names = [flatten_props(e).get("user_name") for e in entries_of(r["data"])]
    names = [n for n in names if n]
    dm_user_name = names[0] if names else user
    print(f"    user_name = {dm_user_name!r}")

    # ---- 3. inbox 原始字段 ----
    print("[3] 查 inbox: select * from dmi_queue_item")
    r2 = dql(f"select * from dmi_queue_item "
             f"where name = '{dm_user_name}' and delete_flag = false "
             f"order by date_sent desc", page_size=5)
    save("03_queue_items", r2)
    if not r2["ok"]:
        print(f"    失败: status={r2['status']}，见 api_03_queue_items.json")
        return 1
    items = entries_of(r2["data"])
    print(f"    取到 {len(items)} 条（只存前 5 条做结构分析）")
    if items:
        cols = sorted(flatten_props(items[0]).keys())
        print(f"    dmi_queue_item 字段: {', '.join(cols)}")

    # ---- 4. 计数，跟界面对账 ----
    print("[4] inbox 计数 count(*)")
    r3 = dql(f"select count(*) as c from dmi_queue_item "
             f"where name = '{dm_user_name}' and delete_flag = false", page_size=1)
    save("04_queue_count", r3)
    if r3["ok"]:
        cnt = [flatten_props(e) for e in entries_of(r3["data"])]
        print(f"    inbox 总条数: {cnt}  <- 跟 D2 界面右上角的数字对一下")

    # ---- 5. 展开任务挂的对象，找 Identifier / Sending / Receiving ----
    print("[5] 展开前 3 条任务挂的对象，dump 全部属性（定字段映射）")
    detail = []
    for it in items[:3]:
        p = flatten_props(it)
        oid = p.get("item_id") or p.get("router_id")
        otype = p.get("item_type")
        if not oid:
            continue
        if not otype:
            t = dql(f"select r_object_type from dm_sysobject "
                    f"where r_object_id = '{oid}'", page_size=1)
            for e in entries_of(t.get("data")):
                otype = flatten_props(e).get("r_object_type")
        full = dql(f"select * from {otype or 'dm_sysobject'} "
                   f"where r_object_id = '{oid}'", page_size=1)
        detail.append({"queue_item_props": p, "item_id": oid,
                       "item_type": otype, "attached_object": full})
    save("05_item_details", detail)
    print(f"    已导出 {len(detail)} 条明细 -> probe_out\\api_05_item_details.json")

    # ---- 6. 顺手看 workitem 侧（due_date / supervisor 可能在这里）----
    print("[6] 顺手查 dmi_workitem（可选，找 due_date/performer）")
    r4 = dql(f"select * from dmi_workitem where r_performer_name = '{dm_user_name}' "
             f"and r_runtime_state in (0,1,2)", page_size=3)
    save("06_workitems", r4)
    print(f"    workitem 查询 status={r4['status']}")

    print("\n完成。probe_out\\api_01..06 已生成（含业务数据、无密码，已被 .gitignore 挡住）。")
    print("请把这几个文件留在文件夹里告我一声，我据此定稿字段映射并写 M1 取数模块。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
