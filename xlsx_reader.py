"""Minimal .xlsx reader using only the standard library (no openpyxl needed)."""
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref).group()
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_workbook(path):
    """Return {sheet_name: [row lists]} with numbers as float and text as str."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))

        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels.iter(f"{REL_NS}Relationship")}
        wb = ET.fromstring(z.read("xl/workbook.xml"))

        sheets = {}
        for s in wb.find("m:sheets", NS):
            target = targets[s.get(f"{{{NS['r']}}}id")].lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            root = ET.fromstring(z.read(target))
            rows = []
            for row in root.iter(f"{{{NS['m']}}}row"):
                cells = {}
                for c in row.findall("m:c", NS):
                    t = c.get("t")
                    v = c.find("m:v", NS)
                    if t == "s" and v is not None:
                        val = shared[int(v.text)]
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(f"{{{NS['m']}}}t"))
                    elif v is None:
                        val = None
                    elif t in ("str", "e"):
                        val = v.text
                    elif t == "b":
                        val = v.text == "1"
                    else:
                        val = float(v.text)
                    cells[_col_index(c.get("r"))] = val
                if cells:
                    rows.append([cells.get(i) for i in range(max(cells) + 1)])
            sheets[s.get("name")] = rows
        return sheets


def excel_date(value):
    """Convert an Excel serial date (or a date string) to a datetime."""
    if isinstance(value, (int, float)):
        return datetime(1899, 12, 30) + timedelta(days=value)
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            pass
    return None


if __name__ == "__main__":
    import sys
    for name, rows in read_workbook(sys.argv[1]).items():
        print(f"== {name}: {len(rows)} rows")
        for r in rows[: int(sys.argv[2]) if len(sys.argv) > 2 else 15]:
            print(r)
