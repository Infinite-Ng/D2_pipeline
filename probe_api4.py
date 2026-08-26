r"""
D2 REST 探测（M0e —— 打开 docmail 虚拟文档的子节点/附件，只读 GET）
======================================================================
M0d 已确认：业务文档是 docmail（虚拟文档），object_name=识别号、title=主题，
其主内容只是一小段 htm（信函正文）。真正的"附件"是虚拟文档的子节点/组件。

本脚本对前 2 个 docmail，逐个打开这些关系并 dump 原始结构（只读 GET）：
  - /objects/{id}/vd-nodes        虚拟文档节点树（子组件）
  - /objects/{id}/d2-vd-nodes     D2 变体
  - /objects/{id}/contents        内容对象列表（主内容 + 渲染件）
  - /objects/{id}/d2-renditions   D2 渲染件
  - /objects/{id}/download-url     D2 给出的下载 URL（可能是打包）
然后启发式收集其中出现的对象 id（16 位 hex），DQL 取它们的
type/name/format/size，并对疑似内容对象做 content-media 下载实测（前 64KB）。
—— 目的：搞清"每条备案实际要附带哪些文件、各多大、什么格式"。

结果写 probe_out\api4_*.json（元数据/结构、无密码、无文档正文）。config.ini 读连接参数。
纯标准库： python probe_api4.py
"""
from __future__ import annotations

import base64
import getpass
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from d2conf import load_d2

_HOST, REPO, _ACCOUNT = load_d2()
BASE = f"https://{_HOST}/D2-REST"
REPO_URL = f"{BASE}/repositories/{urllib.parse.quote(REPO)}"
OUT = Path(__file__).parent / "probe_out"
TIMEOUT = 40
MAX_CONTENT_READ = 64 * 1024
_OID = re.compile(r"\b[0-9a-f]{16}\b")

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE
_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ctx))
_auth = ""


def save(name, obj):
    OUT.mkdir(exist_ok=True)
    (OUT / f"api4_{name}.json").write_text(
        json.dumps(obj, indent=2, ensure_ascii=False)[:800_000], encoding="utf-8")


def _req(url, accept):
    return urllib.request.Request(url, method="GET", headers={
        "Accept": accept, "User-Agent": "D2-probe/5", "Authorization": _auth})


def get_json(url, params=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    try:
        with _opener.open(_req(url, "application/json"), timeout=TIMEOUT) as r:
            text = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, None, e.read().decode("utf-8", "replace")[:1500]
        except Exception:  # noqa: BLE001
            return e.code, None, ""
    except Exception as e:  # noqa: BLE001
        return -1, None, f"{type(e).__name__}: {e}"
    try:
        return 200, json.loads(text), text[:1500]
    except Exception:  # noqa: BLE001
        return 200, None, text[:1500]


def dql(query, page_size=30):
    st, data, _ = get_json(REPO_URL, {"dql": query, "items-per-page": page_size, "page": 1})
    rows = []
    if st == 200 and isinstance(data, dict):
        for e in data.get("entries", []) or []:
            c = e.get("content") if isinstance(e, dict) else None
            p = (c or {}).get("properties") if isinstance(c, dict) else None
            rows.append(p if isinstance(p, dict) else {})
    return rows


def download_probe(url):
    try:
        with _opener.open(_req(url, "*/*"), timeout=TIMEOUT) as r:
            chunk = r.read(MAX_CONTENT_READ)
            return {"status": r.status, "content_type": r.headers.get("Content-Type"),
                    "content_length": r.headers.get("Content-Length"),
                    "bytes_read": len(chunk), "first16_hex": chunk[:16].hex()}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": (e.reason or "")[:150]}
    except Exception as e:  # noqa: BLE001
        return {"status": -1, "error": f"{type(e).__name__}: {e}"}


def esc(s):
    return (s or "").replace("'", "''")


def docmail_ids():
    inbox = dql("select router_id from dmi_queue_item "
                f"where name = '{esc(_user)}' and delete_flag = false order by date_sent desc", 5)
    ids = []
    for r in inbox:
        rid = r.get("router_id")
        if not rid:
            continue
        for pk in dql("select r_component_id from dmi_package "
                      f"where r_workflow_id = '{esc(rid)}'"):
            cid = pk.get("r_component_id")
            for v in (cid if isinstance(cid, list) else [cid]):
                if v and v not in ids:
                    ids.append(v)
    return ids


def main():
    global _auth, _user
    OUT.mkdir(exist_ok=True)
    print(f"目标 {BASE} repo={REPO}\n")
    _user = input(f"    D2 登录名{f' [{_ACCOUNT}]' if _ACCOUNT else ''}: ").strip() or _ACCOUNT
    pwd = getpass.getpass("    D2 密码（不保存、不外传）: ")
    _auth = "Basic " + base64.b64encode(f"{_user}:{pwd}".encode()).decode()

    docmails = docmail_ids()[:2]
    print(f"探查 {len(docmails)} 个 docmail: {docmails}")

    rels = ["vd-nodes", "d2-vd-nodes", "contents", "d2-renditions", "download-url"]
    report = []
    child_ids = set()
    for did in docmails:
        entry = {"docmail": did, "endpoints": {}}
        for rel in rels:
            st, data, raw = get_json(f"{REPO_URL}/objects/{did}/{rel}")
            entry["endpoints"][rel] = {"status": st, "json": data if data is not None else raw}
            found = {m for m in _OID.findall(json.dumps(data) if data is not None else raw)}
            child_ids |= found
        report.append(entry)
        print(f"  {did}: " + ", ".join(
            f"{rel}={entry['endpoints'][rel]['status']}" for rel in rels))
    save("A_relations", report)

    child_ids -= set(docmails)
    child_ids = sorted(child_ids)
    print(f"\n收集到 {len(child_ids)} 个候选对象 id，取元数据...")
    metas = []
    if child_ids:
        idlist = ",".join(f"'{esc(x)}'" for x in child_ids[:40])
        metas = dql("select r_object_id, r_object_type, object_name, title, a_content_type, "
                    f"r_full_content_size, i_has_folder, r_is_virtual_doc "
                    f"from dm_sysobject where r_object_id in ({idlist})", 40)
    save("B_child_meta", metas)
    for m in metas:
        print(f"    {m.get('object_name')!r} type={m.get('r_object_type')} "
              f"fmt={m.get('a_content_type')} size={m.get('r_full_content_size')} "
              f"vdoc={m.get('r_is_virtual_doc')}")

    # 对有内容(size>0 且非纯 htm 壳)的子对象做下载实测
    print("\n下载实测有内容的子对象（前 64KB）...")
    dls = []
    for m in metas:
        size = m.get("r_full_content_size") or 0
        cid = m.get("r_object_id")
        if not cid or not size:
            continue
        d = download_probe(f"{REPO_URL}/objects/{cid}/content-media")
        dls.append({"id": cid, "object_name": m.get("object_name"),
                    "a_content_type": m.get("a_content_type"), "size": size, "download": d})
        print(f"    {m.get('object_name')} [{m.get('a_content_type')}] "
              f"size={size} -> {d.get('status')} {d.get('content_type')}")
    save("C_child_download", dls)

    print("\n完成。probe_out\\api4_*.json 已生成。把它们发我，我据此定附件方案。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
