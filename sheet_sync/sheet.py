"""Google Sheet read/diff/write. Every write passes through the hard allowlist."""

import csv
import re
from datetime import datetime
from pathlib import Path

HEADER = [
    "Team Name", "Team Manager", "Opponent Name", "Opponent Manager", "Projected",
    "Opponent Projected", "Projected Margin", "PF", "PA", "W/L/T", "Margin",
    "Week MVP", "MVP Points", "Bench MVP", "Bench MVP Points",
]
COLUMNS = "ABCDEFGHIJKLMNO"
WRITABLE_COLUMNS = frozenset("CEHLMNO")
WRITABLE_ROWS = range(2, 16)
# More opponent mismatches than this means a wrong team map or week, not a typo.
MAX_SCHEDULE_MISMATCHES = 3
WEEK_TAB = re.compile(r"^Week ([1-9]|1[0-4])$")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class AllowlistError(Exception):
    pass


class SheetError(Exception):
    pass


def check_allowed(tab, row, col):
    if not WEEK_TAB.match(tab):
        raise AllowlistError(f"Writes to tab {tab!r} are not allowed (only 'Week 1'..'Week 14')")
    if col not in WRITABLE_COLUMNS:
        raise AllowlistError(f"Writes to column {col} are not allowed (only C, E, H, L-O)")
    if row not in WRITABLE_ROWS:
        raise AllowlistError(f"Writes to row {row} are not allowed (only rows 2-15)")


def validate_header(row1):
    actual = [str(v).strip() for v in row1] + [""] * (len(HEADER) - len(row1))
    actual = actual[: len(HEADER)]
    if actual != HEADER:
        bad = [f"{COLUMNS[i]}1: expected {e!r}, found {a!r}" for i, (e, a) in enumerate(zip(HEADER, actual)) if e != a]
        raise SheetError("Header row does not match spec; refusing to write. " + "; ".join(bad))


def cell_value(grid, row, col):
    """grid is the A1:O15 values as returned by the API (ragged lists)."""
    r = row - 1
    c = COLUMNS.index(col)
    if r >= len(grid) or c >= len(grid[r]):
        return ""
    return grid[r][c]


def _norm(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = str(v).strip()
    try:
        return round(float(s), 2)
    except ValueError:
        return s


def values_equal(old, new):
    a, b = _norm(old), _norm(new)
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) < 0.005
    return a == b


def diff(grid, cells):
    """[(a1, old, new)] for intended cells that differ from the sheet."""
    changes = []
    for (row, col), new in sorted(cells.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        old = cell_value(grid, row, col)
        if not values_equal(old, new):
            changes.append((f"{col}{row}", old, new))
    return changes


def formula_cells(formula_grid, cells):
    return [
        f"{col}{row}"
        for (row, col) in cells
        if str(cell_value(formula_grid, row, col)).startswith("=")
    ]


def schedule_mismatches(grid, cells):
    """Pre-filled column C values that disagree with ESPN's schedule."""
    out = []
    for (row, col), new in sorted(cells.items()):
        if col != "C":
            continue
        old = cell_value(grid, row, col)
        if str(old).strip() and not values_equal(old, new):
            out.append(f"C{row}: sheet has opponent {old!r}, ESPN schedule says {new!r}")
    return out


def e_column_stats(grid, cells):
    """Absolute differences between current and intended column E values."""
    diffs, blank = [], 0
    for (row, col), new in cells.items():
        if col != "E":
            continue
        old = _norm(cell_value(grid, row, col))
        if not isinstance(old, float):
            blank += 1
            continue
        diffs.append(abs(old - float(new)))
    return {
        "compared": len(diffs),
        "max": max(diffs) if diffs else 0.0,
        "mean": sum(diffs) / len(diffs) if diffs else 0.0,
        "blank": blank,
    }


def write_backup(tab, rows, backup_dir="backups"):
    path = Path(backup_dir)
    path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = path / f"{tab.replace(' ', '_')}_{stamp}.csv"
    with out.open("w", newline="") as f:
        csv.writer(f).writerows(rows)
    return out


def _http_error(e):
    status = getattr(getattr(e, "resp", None), "status", None)
    if status == 429:
        return SheetError("Google Sheets API quota exceeded (HTTP 429). Wait and re-run.")
    if status == 403:
        return SheetError("Google Sheets permission denied (HTTP 403). Share the sheet with the service account email as Editor.")
    if status == 404:
        return SheetError("Spreadsheet not found (HTTP 404). Check GOOGLE_SHEET_ID.")
    return SheetError(f"Google Sheets API error: {e}")


class SheetClient:
    def __init__(self, spreadsheet_id, service_account_info=None, service=None):
        self.spreadsheet_id = spreadsheet_id
        if service is None:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build

            try:
                creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
            except ValueError:
                raise SheetError("Service-account key is missing required fields (GOOGLE_SERVICE_ACCOUNT_JSON)")
            service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.values = service.spreadsheets().values()

    def _call(self, request):
        from google.auth.exceptions import GoogleAuthError
        from googleapiclient.errors import HttpError

        try:
            return request.execute()
        except HttpError as e:
            raise _http_error(e)
        except GoogleAuthError as e:
            raise SheetError(f"Google service-account auth failed ({type(e).__name__}). Check GOOGLE_SERVICE_ACCOUNT_JSON.")

    def read_week(self, tab):
        """Return (unformatted grid, formula grid) for A1:O15."""
        rng = f"'{tab}'!A1:O15"
        vals = self._call(self.values.get(spreadsheetId=self.spreadsheet_id, range=rng, valueRenderOption="UNFORMATTED_VALUE"))
        forms = self._call(self.values.get(spreadsheetId=self.spreadsheet_id, range=rng, valueRenderOption="FORMULA"))
        return vals.get("values", []), forms.get("values", [])

    def read_tab(self, tab):
        resp = self._call(self.values.get(spreadsheetId=self.spreadsheet_id, range=f"'{tab}'", valueRenderOption="FORMULA"))
        return resp.get("values", [])

    def write_cells(self, tab, cells):
        """One batch update, RAW values only. cells: {(row, col): value}."""
        data = []
        for (row, col), value in sorted(cells.items()):
            check_allowed(tab, row, col)
            data.append({"range": f"'{tab}'!{col}{row}", "values": [[value]]})
        if not data:
            return 0
        body = {"valueInputOption": "RAW", "data": data}
        resp = self._call(self.values.batchUpdate(spreadsheetId=self.spreadsheet_id, body=body))
        return resp.get("totalUpdatedCells", 0)
