"""
极简 .xlsx 写入器（纯标准库，无需 openpyxl）。
.xlsx 本质是一个 zip 包着几个 XML；这里用 inlineStr 存字符串、<v> 存数字，
避免 sharedStrings/styles 的复杂度，产出可被 Excel/WPS 正常打开的工作簿。

用法：
    write_workbook("out.xlsx", [("Items", rows1), ("Summary", rows2)])
    rows = [[表头...], [值...], ...]   值可为 str / int / float / None
"""
from __future__ import annotations

import zipfile

_CT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    "{sheet_overrides}"
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)


def _col(idx: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA ..."""
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def _esc(v) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _cell(ref: str, value) -> str:
    if isinstance(value, bool):  # bool 归到字符串，避免歧义
        value = "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    if value is None:
        value = ""
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_esc(value)}</t></is></c>'


def _sheet_xml(rows: list) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>',
    ]
    for r, row in enumerate(rows, 1):
        cells = "".join(_cell(f"{_col(c)}{r}", v) for c, v in enumerate(row))
        parts.append(f'<row r="{r}">{cells}</row>')
    parts.append("</sheetData></worksheet>")
    return "".join(parts)


def write_workbook(path: str, sheets: list[tuple[str, list]]) -> str:
    """sheets: [(sheet_name, rows), ...]；rows: list[list[cell]]。返回 path。"""
    if not sheets:
        sheets = [("Sheet1", [])]
    overrides, wb_sheets, wb_rels = [], [], []
    for i, (name, _rows) in enumerate(sheets, 1):
        overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
        wb_sheets.append(f'<sheet name="{_esc(name)[:31]}" sheetId="{i}" r:id="rId{i}"/>')
        wb_rels.append(
            f'<Relationship Id="rId{i}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
        )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{"".join(wb_sheets)}</sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{"".join(wb_rels)}</Relationships>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT.format(sheet_overrides="".join(overrides)))
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        for i, (_name, rows) in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows))
    return path
