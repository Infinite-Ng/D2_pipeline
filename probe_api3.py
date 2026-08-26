r"""
D2 REST 探测（M0d —— 附件/内容下载链路，只读 GET，不发邮件、不改任何东西）
================================================================================
目的：确认"把 inbox 条目对应的 D2 文档下载下来"这条路怎么走，为"邮件附带可下载附件"打基础。

链路：dmi_queue_item.router_id ─▶ dm_workflow ─▶ dmi_package.r_component_id ─▶ 业务文档 ─▶ content

脚本做的事（全部只读 GET）：
  A. 取 inbox 前 5 条（router_id / task_subject）
  B. 每条查 dmi_package(select *) 拿 r_component_id（业务文档 id，可能多个/repeating）
  C. 业务文档元数据：DQL 取 r_object_type / object_name / a_content_type /
     r_full_content_size / i_has_folder / r_is_virtual_doc（判断单文件 or 虚拟文档）
  D. content 下载实测：GET /objects/{id}，从 links 里找 content-media/enclosure，
     GET 它并**只读前 64KB** —— 确认能下、拿到 Content-Type/大小/文件头魔数
     （不落盘文档内容，只存元数据 + 前 16 字节 hex）
  E. 格式映射：查 dm_format，把 a_content_type 映射到扩展名/MIME（决定附件文件名）

结果写到 probe_out\api3_*.json（含业务元数据、无密码、无文档正文，已被 .gitignore 挡住）。
内网主机名/仓库/账号从 config.ini 读。纯标准库，直接： python probe_api3.py
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

_HOST, REPO, _ACCOUNT = load_d2()
BASE = f"https://{_HOST}/D2-REST"
OUT = Path(__file__).parent / "probe_out"
TIMEOUT = 40
MAX_CONTENT_READ = 64 * 1024  # 下载实测只读前 64KB，确认机制即可

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE
_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ctx))  # 跟随重定向(ACS)
_auth = ""


def save(name: str, obj) -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / f"api3_{name}.json").write_text(
        json.dumps(obj, indent=2, ensure_ascii=False)[:800_000], encoding="utf-8")


def _req(url: str, accept: str) -> urllib.request.Request:
    return urllib.request.Request(url, method="GET", headers={
        "Accept": accept, "User-Agent": "D2-probe/4", "Authorization": _auth})


def get_json(url: str, params: dict | None = None) -> tuple[int, dict | None, str]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    try:
        with _opener.open(_req(url, "application/json"), timeout=TIMEOUT) as r:
            text = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, None, e.read().decode("utf-8", "replace")[:2000]
        except Exception:  # noqa: BLE001
            return e.code, None, ""
    except Exception as e:  # noqa: BLE001
        return -1, None, f"{type(e).__name__}: {e}"
    try:
        return 200, json.loads(text), ""
    except Exception:  # noqa: BLE001
        return 200, None, text[:2000]


def dql(query: str, page_size: int = 20) -> list[dict]:
    st, data, _ = get_json(f"{BASE}/repositories/{urllib.parse.quote(REPO)}",
                           {"dql": query, "items-per-page": page_size, "page": 1})
    rows = []
    if st == 200 and isinstance(data, dict):
        for e in data.get("entries", []) or []:
            c = e.get("content") if isinstance(e, dict) else None
            p = (c or {}).get("properties") if isinstance(c, dict) else None
            rows.append(p if isinstance(p, dict) else {})
    return rows


def download_probe(url: str) -> dict:
    """GET 一个 content 媒体 URL，只读前 64KB，回报元数据（不保存正文）。"""
    try:
        with _opener.open(_req(url, "*/*"), timeout=TIMEOUT) as r:
            chunk = r.read(MAX_CONTENT_READ)
            return {"url": url, "status": r.status,
                    "content_type": r.headers.get("Content-Type"),
                    "content_length": r.headers.get("Content-Length"),
                    "bytes_read": len(chunk),
                    "first16_hex": chunk[:16].hex(),
                    "looks_like": _magic(chunk)}
    except urllib.error.HTTPError as e:
        return {"url": url, "status": e.code, "error": (e.reason or "")[:200]}
    except Exception as e:  # noqa: BLE001
        return {"url": url, "status": -1, "error": f"{type(e).__name__}: {e}"}


def _magic(b: bytes) -> str:
    if b[:4] == b"%PDF":
        return "pdf"
    if b[:2] == b"PK":
        return "zip/ooxml(docx/xlsx/pptx)"
    if b[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole(doc/xls/ppt)"
    if b[:5] == b"{\\rtf":
        return "rtf"
    return "unknown"


def _links_of(obj: dict) -> list[dict]:
    return obj.get("links", []) if isinstance(obj, dict) else []


def main() -> int:
    global _auth
    OUT.mkdir(exist_ok=True)
    print(f"目标 {BASE} repo={REPO}\n")
    user = input(f"    D2 登录名{f' [{_ACCOUNT}]' if _ACCOUNT else ''}: ").strip() or _ACCOUNT
    pwd = getpass.getpass("    D2 密码（不保存、不外传、我看不到）: ")
    _auth = "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()

    def esc(s):
        return (s or "").replace("'", "''")

    # A. inbox 前 5 条
    print("[A] 取 inbox 前 5 条")
    inbox = dql("select r_object_id, item_id, router_id, task_subject "
                f"from dmi_queue_item where name = '{esc(user)}' and delete_flag = false "
                "order by date_sent desc", page_size=5)
    save("A_inbox", inbox)
    routers = [r.get("router_id") for r in inbox if r.get("router_id")]
    print(f"    {len(inbox)} 条，{len(routers)} 个 workflow")

    # B. workflow -> package -> component id
    print("[B] 查 dmi_package -> 业务文档 id")
    pkgs, comp_ids = [], []
    for rid in routers[:3]:
        # r_component_id 是 repeating 属性，select * 不含它，必须显式列出
        rows = dql("select r_object_id, r_component_id, r_package_name, r_package_type "
                   f"from dmi_package where r_workflow_id = '{esc(rid)}'")
        pkgs.append({"router_id": rid, "packages": rows})
        for r in rows:
            cid = r.get("r_component_id")
            for v in (cid if isinstance(cid, list) else [cid]):
                if v and v not in comp_ids:
                    comp_ids.append(v)
    save("B_packages", pkgs)
    print(f"    收集到 {len(comp_ids)} 个业务文档 id")
    if not comp_ids:
        print("    没解析到业务文档 id —— 把 api3_A/api3_B 发我，我看结构再调。")
        return 1

    # C. 业务文档元数据
    print("[C] 业务文档元数据（类型/大小/格式/是否虚拟文档）")
    idlist = ",".join(f"'{esc(x)}'" for x in comp_ids[:8])
    metas = dql("select r_object_id, r_object_type, object_name, title, a_content_type, "
                "r_full_content_size, r_content_size, i_has_folder, r_is_virtual_doc "
                f"from dm_sysobject where r_object_id in ({idlist})", page_size=20)
    save("C_component_meta", metas)
    for m in metas:
        print(f"    {m.get('object_name')!r}  type={m.get('r_object_type')}  "
              f"fmt={m.get('a_content_type')}  size={m.get('r_full_content_size')}  "
              f"vdoc={m.get('r_is_virtual_doc')}")

    # D. content 下载实测（前 64KB）
    print("[D] content 下载实测（只取前 64KB）")
    dl = []
    for cid in comp_ids[:5]:
        st, obj, raw = get_json(f"{BASE}/repositories/{urllib.parse.quote(REPO)}/objects/{cid}")
        links = _links_of(obj) if obj else []
        media = next((l.get("href") for l in links
                      if isinstance(l, dict) and (
                          "content-media" in (l.get("rel") or "") or l.get("rel") == "enclosure")),
                     None)
        entry = {"component_id": cid, "object_status": st,
                 "links": [{"rel": l.get("rel"), "href": l.get("href")} for l in links],
                 "media_link": media}
        if media:
            entry["download"] = download_probe(media)
        else:
            # 退一步试约定路径
            entry["download"] = download_probe(
                f"{BASE}/repositories/{urllib.parse.quote(REPO)}/objects/{cid}/content-media")
        dl.append(entry)
        d = entry["download"]
        print(f"    {cid}: media={'有' if media else '猜'} -> status={d.get('status')} "
              f"type={d.get('content_type')} size={d.get('content_length')} {d.get('looks_like','')}")
    save("D_download_test", dl)

    # E. 格式 -> 扩展名/MIME
    fmts = sorted({m.get("a_content_type") for m in metas if m.get("a_content_type")})
    if fmts:
        print("[E] 格式映射 dm_format")
        flist = ",".join(f"'{esc(x)}'" for x in fmts)
        rows = dql("select name, dos_extension, mime_type, description "
                   f"from dm_format where name in ({flist})", page_size=50)
        save("E_formats", rows)
        for r in rows:
            print(f"    {r.get('name')} -> .{r.get('dos_extension')}  {r.get('mime_type')}")

    print("\n完成。probe_out\\api3_*.json 已生成（元数据、无密码、无文档正文，已 gitignore）。")
    print("把这些发我，我据此定附件策略并实现下载+附加。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
