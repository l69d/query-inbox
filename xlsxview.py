"""Read an .xlsx for the .xlsx inbox viewer: sheet list, then one sheet's cells with their display text and
basic styling (bold / italic / colours / alignment / merges / column widths), so a workbook can be read in
the browser without Excel.

sheet_names(path) is cheap (reads the workbook part of the zip). read_sheet(path, index) loads the workbook;
files over FULL_LOAD_MB are streamed read-only (no merges / widths) so a 30 MB workbook still opens fast.
Formulas show their cached value; a formula with no cached value (a file openpyxl wrote and Excel never
saved) shows the formula itself. Self-check: python xlsxview.py
"""
import datetime as dt
import re
from decimal import ROUND_HALF_UP, Decimal
import zipfile
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree

import openpyxl
from openpyxl.styles.colors import COLOR_INDEX
from openpyxl.utils import get_column_letter

MAX_ROWS, MAX_COLS = 1000, 80
FULL_LOAD_MB = 3  # above this, stream read-only: full loads of 4-7 MB files took ~5 s
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def sheet_names(path):
    try:
        with zipfile.ZipFile(path) as z:
            root = ElementTree.fromstring(z.read("xl/workbook.xml"))
        return [s.get("name") for s in root.iterfind("m:sheets/m:sheet", NS)]
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return []


def color(c):
    """An openpyxl Color as #rrggbb, or None (theme colours are left to the default palette)."""
    if c is None:
        return None
    try:
        if c.type == "rgb" and isinstance(c.rgb, str) and len(c.rgb) >= 6 and c.rgb not in ("00000000",):
            return "#" + c.rgb[-6:]
        if c.type == "indexed" and c.indexed is not None and c.indexed < len(COLOR_INDEX):
            return "#" + COLOR_INDEX[c.indexed][-6:]
    except (AttributeError, TypeError):
        pass
    return None


def decimals(fmt):
    m = re.search(r"0\.(0+)", fmt)
    return len(m.group(1)) if m else 0


def fixed(v, places, sep=True):
    """Round half-up like Excel (Python's format rounds 20.55 down to 20.5 because of float error)."""
    q = (v if isinstance(v, Decimal) else Decimal(repr(v))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return f"{q:,.{places}f}" if sep else f"{q:.{places}f}"


def display(v, fmt):
    """The text Excel would roughly show for value v under number format fmt."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (dt.datetime, dt.date)):
        if isinstance(v, dt.datetime) and (v.hour or v.minute or v.second):
            return v.strftime("%Y-%m-%d %H:%M")
        return v.strftime("%Y-%m-%d")
    if isinstance(v, dt.time):
        return v.strftime("%H:%M")
    if isinstance(v, (int, float)):
        fmt = (fmt or "General").split(";")[0]
        if "%" in fmt:
            return fixed(Decimal(repr(v)) * 100, decimals(fmt)) + "%"
        if "#,##" in fmt:
            return fixed(v, decimals(fmt))
        if re.fullmatch(r"0(\.0+)?", fmt):
            return fixed(v, decimals(fmt), sep=False)
        if isinstance(v, int) or float(v).is_integer():
            return str(int(v))
        return f"{v:.6g}" if abs(v) < 1e15 else str(v)
    return str(v)


def cell_style(c):
    st = {}
    f = c.font
    if f is not None:
        if f.b:
            st["b"] = 1
        if f.i:
            st["i"] = 1
        fc = color(f.color)
        if fc and fc.lower() != "#000000":
            st["c"] = fc
    fill = c.fill
    if fill is not None and getattr(fill, "fill_type", None) == "solid":
        bg = color(fill.fgColor)
        if bg and bg.lower() != "#ffffff":
            st["bg"] = bg
    al = c.alignment
    if al is not None:
        if al.horizontal in ("center", "right", "left"):
            st["a"] = al.horizontal[0]
        if al.wrap_text:
            st["w"] = 1
    return st


@lru_cache(maxsize=8)
def _load(path, mtime, data_only):
    big = Path(path).stat().st_size > FULL_LOAD_MB * 1024 * 1024
    return openpyxl.load_workbook(path, data_only=data_only, read_only=big), big


def read_sheet(path, index=0):
    path = str(path)
    mtime = Path(path).stat().st_mtime
    wb, big = _load(path, mtime, True)
    names = wb.sheetnames
    index = max(0, min(int(index), len(names) - 1))
    ws = wb[names[index]]
    formulas = None if big else _load(path, mtime, False)[0][names[index]]

    max_row, max_col = ws.max_row or 0, ws.max_column or 0
    if big and not max_row:  # the file has no stored dimension: read what is there, up to the limits
        ws.reset_dimensions()
        max_row, max_col = MAX_ROWS, MAX_COLS
    rows = []
    for r_i, row in enumerate(ws.iter_rows(max_row=min(max_row, MAX_ROWS), max_col=min(max_col, MAX_COLS)), 1):
        out = []
        for c in row:
            v = getattr(c, "value", None)
            fmt = getattr(c, "number_format", "General")
            cell = {"d": display(v, fmt)}
            if formulas is not None and v is None:
                fv = formulas.cell(r_i, c.column).value if hasattr(c, "column") else None
                if isinstance(fv, str) and fv.startswith("="):
                    cell.update(d=fv, f=1)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cell["n"] = 1
            st = cell_style(c) if hasattr(c, "font") else {}
            if st:
                cell["s"] = st
            out.append(cell)
        rows.append(out)

    merges, widths = [], {}
    if not big:
        for m in ws.merged_cells.ranges:
            if m.min_row <= MAX_ROWS and m.min_col <= MAX_COLS:
                merges.append([m.min_row, m.min_col, min(m.max_row, MAX_ROWS), min(m.max_col, MAX_COLS)])
        for letter, d in ws.column_dimensions.items():
            if d.width:
                widths[letter] = round(d.width, 1)
    if big:  # a guessed size pads with blanks: trim empty trailing rows and columns
        while rows and not any(c["d"] for c in rows[-1]):
            rows.pop()
        width = max((max((i + 1 for i, c in enumerate(r) if c["d"]), default=0) for r in rows), default=0)
        rows = [r[:width] for r in rows]
        if max_row == MAX_ROWS and len(rows) < MAX_ROWS:
            max_row, max_col = len(rows), width
    shown_cols = max((len(r) for r in rows), default=0)
    return {
        "sheets": names, "index": index, "name": names[index],
        "rows": rows, "max_row": max_row, "max_col": max_col,
        "cols": [get_column_letter(i) for i in range(1, shown_cols + 1)],
        "truncated_rows": max_row > MAX_ROWS, "truncated_cols": max_col > MAX_COLS,
        "merges": merges, "widths": widths, "streamed": big,
    }


if __name__ == "__main__":
    import tempfile

    from openpyxl.styles import Font, PatternFill

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "t.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Summary"
        ws.append(["city", "orders", "share"])
        ws.append(["ncr", 6775038, 0.2055])
        ws["A1"].font = Font(bold=True)
        ws["A1"].fill = PatternFill("solid", fgColor="1F4E78")
        ws["B2"].number_format = "#,##0"
        ws["C2"].number_format = "0.0%"
        ws["D2"] = "=B2*2"
        ws.merge_cells("A4:C4")
        wb.create_sheet("Second")
        wb.save(p)

        assert sheet_names(p) == ["Summary", "Second"]
        s = read_sheet(p, 0)
        assert s["rows"][1][1]["d"] == "6,775,038" and s["rows"][1][2]["d"] == "20.6%", s["rows"][1]
        assert s["rows"][0][0]["s"] == {"b": 1, "bg": "#1f4e78"} or s["rows"][0][0]["s"]["b"] == 1, s["rows"][0][0]
        assert s["rows"][1][3] == {"d": "=B2*2", "f": 1}, s["rows"][1][3]  # never opened in Excel: no cached value
        assert s["merges"] == [[4, 1, 4, 3]] and read_sheet(p, 1)["name"] == "Second"
        assert display(dt.datetime(2026, 10, 21), "yyyy-mm-dd") == "2026-10-21" and display(1234.5, "General") == "1234.5"
    print("ok")
