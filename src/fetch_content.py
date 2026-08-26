"""
M2.5 附件层：把每条备案对应 docmail 的真实附件（子文件，如 PDF）只读下载下来，供邮件附带。

链路（全部只读 GET）：
  item.router_id -> dmi_package.r_component_id (docmail 虚拟文档)
                 -> vd-nodes 子组件（真实文件） -> content-media 下载

要点：
  * docmail 是虚拟文档，主内容只是一小段 htm 信函正文；真正附件是它的子节点。
  * 也兼容"组件本身就是带内容的叶子文档"的情况（无子节点时回退附自身）。
  * 逐文件按大小累计，超过上限即跳过并记录（外部 Gmail 收件人有 25MB 限制）。
  * 单条失败只跳过并记录，绝不让整条流水线崩。
只读；不修改 D2 任何内容。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_VDC = "virtual-document-component"
_OID_IN_URL = re.compile(r"/objects/([0-9a-f]{16})")

_META = ("select r_object_id, r_object_type, object_name, title, a_content_type, "
         "r_full_content_size, r_is_virtual_doc from dm_sysobject where r_object_id in ({ids})")

# a_content_type -> 扩展名（多数即扩展名本身，少数需映射）
_EXT = {"msw12": "docx", "excel12book": "xlsx", "ppt12": "pptx",
        "msw8": "doc", "excel8book": "xls", "jpeg": "jpg"}


def _q(s: str) -> str:
    return (s or "").replace("'", "''")


@dataclass
class Attachment:
    object_id: str
    filename: str
    fmt: str
    size: int
    saved_path: str | None = None


@dataclass
class ItemAttachments:
    identifier: str | None
    attached: list = field(default_factory=list)   # 已下载的 Attachment
    omitted: list = field(default_factory=list)     # 因超限/失败跳过的 Attachment
    error: str | None = None


def _meta(client, ids: list[str]) -> dict:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    idlist = ",".join(f"'{_q(x)}'" for x in ids)
    rows = client.dql_page(_META.format(ids=idlist), page_size=100)
    return {r.get("r_object_id"): r for r in rows if r.get("r_object_id")}


def _downloadable(m: dict) -> bool:
    return bool(m and m.get("r_object_type") != "dm_folder"
                and (m.get("a_content_type") or "") != ""
                and (m.get("r_full_content_size") or 0) > 0)


def _filename(m: dict) -> str:
    title = (m.get("title") or "").strip()
    if title and "." in title:                 # title 常常就是真实文件名（含扩展名）
        return title
    base = title or (m.get("object_name") or m.get("r_object_id") or "file").strip()
    ext = _EXT.get(m.get("a_content_type") or "", m.get("a_content_type") or "")
    return f"{base}.{ext}" if ext else base


def _att(m: dict) -> Attachment:
    return Attachment(object_id=m.get("r_object_id"), filename=_filename(m),
                      fmt=m.get("a_content_type") or "",
                      size=int(m.get("r_full_content_size") or 0))


def _child_ids_from_vdnodes(vd_json, exclude: str) -> list[str]:
    """遍历 vd-nodes 响应，抽出所有 virtual-document-component 链接指向的子对象 id。"""
    ids: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for lk in node.get("links", []) or []:
                if isinstance(lk, dict) and _VDC in (lk.get("rel") or ""):
                    m = _OID_IN_URL.search(lk.get("href") or "")
                    if m and m.group(1) != exclude and m.group(1) not in ids:
                        ids.append(m.group(1))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(vd_json)
    return ids


def resolve_component_ids(client, router_id: str) -> list[str]:
    """workflow -> dmi_package.r_component_id（业务 docmail id）。"""
    rows = client.dql_page(
        f"select r_component_id from dmi_package where r_workflow_id = '{_q(router_id)}'",
        page_size=100)
    ids: list[str] = []
    for r in rows:
        cid = r.get("r_component_id")
        for v in (cid if isinstance(cid, list) else [cid]):
            if v and v not in ids:
                ids.append(v)
    return ids


def attachments_for_component(client, comp_id: str) -> list[Attachment]:
    """docmail 组件 -> 真实附件列表（未下载）。虚拟文档取子节点；否则回退自身内容。"""
    meta = _meta(client, [comp_id]).get(comp_id, {})
    out: list[Attachment] = []
    if meta.get("r_is_virtual_doc"):
        vd = client.get_json_url(client.object_url(comp_id, "vd-nodes"))
        child_ids = _child_ids_from_vdnodes(vd, exclude=comp_id) if vd else []
        cmeta = _meta(client, child_ids)
        seen = set()
        for cid in child_ids:
            m = cmeta.get(cid, {})
            if _downloadable(m) and cid not in seen:
                seen.add(cid)
                out.append(_att(m))
    if not out and _downloadable(meta):    # 无子附件但自身有内容
        out.append(_att(meta))
    return out


def _unique(used: set, name: str) -> str:
    if name not in used:
        used.add(name)
        return name
    stem, dot, ext = name.rpartition(".")
    i = 2
    while True:
        cand = f"{stem}_{i}.{ext}" if dot else f"{name}_{i}"
        if cand not in used:
            used.add(cand)
            return cand
        i += 1


def collect_for_items(client, items, dest_dir: str, max_total_bytes: int) -> tuple[list, list, int]:
    """
    对每个 item 解析并下载附件，落到 dest_dir。返回 (per_item, all_saved_paths, total_bytes)。
    超过 max_total_bytes 的文件跳过并计入 omitted。
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    per_item: list[ItemAttachments] = []
    used_names: set = set()
    total = 0
    for it in items:
        ia = ItemAttachments(identifier=getattr(it, "identifier", None))
        try:
            router = getattr(it, "router_id", None)
            comp_ids = resolve_component_ids(client, router) if router else []
            atts, seen = [], set()
            for cid in comp_ids:
                for a in attachments_for_component(client, cid):
                    if a.object_id not in seen:
                        seen.add(a.object_id)
                        atts.append(a)
            for a in atts:
                if total + a.size > max_total_bytes:
                    ia.omitted.append(a)
                    continue
                try:
                    data = client.download(client.object_url(a.object_id, "content-media"))
                except Exception as exc:  # noqa: BLE001
                    a.saved_path = None
                    ia.omitted.append(a)
                    continue
                fname = _unique(used_names, a.filename)
                (dest / fname).write_bytes(data)
                a.saved_path = str(dest / fname)
                a.size = len(data)
                total += len(data)
                ia.attached.append(a)
        except Exception as exc:  # noqa: BLE001
            ia.error = f"{type(exc).__name__}: {exc}"
        per_item.append(ia)
    all_paths = [a.saved_path for ia in per_item for a in ia.attached if a.saved_path]
    return per_item, all_paths, total
