"""Read-only before/after integrity check around a sync write.

    python -m sheet_sync.verify snapshot --out before.json
    python -m sheet_sync --week N --commit
    python -m sheet_sync.verify compare --before before.json --week N [--after after.json] [--expected E2,L8]

`compare` without --after takes the after-snapshot now (saved next to --before).
It checks that, compared with the before-snapshot:
  - on `Week N`, formula-render changes are only in C, E, H, L-O rows 2-15 (and equal --expected if given);
  - no other tab's formula render changed;
  - no new error values (#REF!, #NAME?, #VALUE!, #DIV/0!, #N/A, ...) appeared on any tab.
Uses the spreadsheets.readonly scope. Production needs --allow-production (read-only, same gate as the writer).
"""

import argparse
import json
import re
import sys
from pathlib import Path

from . import config, sheet

READONLY_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
RENDERS = ("FORMULA", "UNFORMATTED_VALUE")
ERRORS = ("#REF!", "#NAME?", "#VALUE!", "#DIV/0!", "#N/A", "#ERROR!", "#NUM!", "#NULL!")
AWARD_ROWS = (17, 36)


class VerifyError(Exception):
    pass


def readonly_service(info):
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_info(info, scopes=READONLY_SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def take_snapshot(service, spreadsheet_id):
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title").execute()
    tabs = [s["properties"]["title"] for s in meta["sheets"]]
    snap = {"tabs": tabs}
    for render in RENDERS:
        resp = service.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id, ranges=[f"'{t}'" for t in tabs], valueRenderOption=render
        ).execute()
        snap[render] = {t: vr.get("values", []) for t, vr in zip(tabs, resp["valueRanges"])}
    return snap


def col_name(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cells(grid):
    return {(r, col_name(c)): v for r, row in enumerate(grid, start=1) for c, v in enumerate(row) if v != ""}


def _changed(a, b):
    ca, cb = _cells(a), _cells(b)
    keys = [k for k in set(ca) | set(cb) if ca.get(k, "") != cb.get(k, "")]
    return sorted(keys, key=lambda k: (k[0], len(k[1]), k[1]))


def _errors(grid, rows=None):
    return {
        k: v for k, v in _cells(grid).items()
        if isinstance(v, str) and v.strip().startswith(ERRORS) and (rows is None or rows[0] <= k[0] <= rows[1])
    }


def _a1(k):
    return f"{k[1]}{k[0]}"


def compare(before, after, week, expected=None):
    """Return (ok, report lines)."""
    tab = f"Week {week}"
    if tab not in before["tabs"] or tab not in after["tabs"]:
        raise VerifyError(f"Tab {tab!r} missing from a snapshot")
    lines, ok = [], True

    changed = _changed(before["FORMULA"][tab], after["FORMULA"][tab])
    names = [_a1(k) for k in changed]
    outside = [_a1(k) for k in changed if k[1] not in sheet.WRITABLE_COLUMNS or k[0] not in sheet.WRITABLE_ROWS]
    lines.append(f"[{tab}] formula-render cells changed: {len(changed)}: {', '.join(names) or 'none'}")
    lines.append(f"[{tab}] changes outside C,E,H,L-O rows 2-15: {outside or 'none'}")
    ok &= not outside
    if expected is not None:
        exp = set(expected)
        if set(names) != exp:
            ok = False
            lines.append(f"[{tab}] FAIL vs expected: unexpected {sorted(set(names) - exp)}, missing {sorted(exp - set(names))}")
        else:
            lines.append(f"[{tab}] matches the {len(exp)} expected cell(s)")

    other = {t: len(_changed(before["FORMULA"][t], after["FORMULA"].get(t, []))) for t in before["tabs"] if t != tab}
    touched = {t: n for t, n in other.items() if n}
    added = sorted(set(after["tabs"]) - set(before["tabs"]))
    lines.append(f"[other tabs] formula-render changes: {touched or 'none'} ({len(other)} tabs checked); tabs added: {added or 'none'}")
    ok &= not touched and not added

    for label, t, rows in ((f"{tab} rows 17-36", tab, AWARD_ROWS), ("2026", "2026", None)):
        if t not in before["tabs"]:
            continue
        eb, ea = _errors(before["UNFORMATTED_VALUE"][t], rows), _errors(after["UNFORMATTED_VALUE"][t], rows)
        new = {_a1(k): v for k, v in ea.items() if eb.get(k) != v}
        cleared = sorted(_a1(k) for k in eb if k not in ea)
        lines.append(f"[{label}] errors before: {len(eb)}; after: {len(ea)}; new: {new or 'none'}; cleared: {cleared or 'none'}")

    new_any = {}
    for t in before["tabs"]:
        eb, ea = _errors(before["UNFORMATTED_VALUE"][t]), _errors(after["UNFORMATTED_VALUE"].get(t, []))
        n = [_a1(k) for k, v in ea.items() if eb.get(k) != v]
        if n:
            new_any[t] = n
    lines.append(f"[all tabs] new error cells: {new_any or 'none'}")
    ok &= not new_any

    vchanged = {t: len(_changed(before["UNFORMATTED_VALUE"][t], after["UNFORMATTED_VALUE"].get(t, []))) for t in before["tabs"]}
    lines.append(f"[info] computed values changed per tab: {({t: n for t, n in vchanged.items() if n}) or 'none'}")
    lines.append("RESULT: " + ("PASS" if ok else "FAIL"))
    return ok, lines


def _target(args):
    sid = config.sheet_id()
    is_production = sid == config.DEFAULT_SHEET_ID
    print(f"Sheet: ...{sid[-6:]} ({'PRODUCTION' if is_production else 'test copy'}) [read-only]")
    if is_production and not args.allow_production:
        raise VerifyError("Refusing to read the production sheet without --allow-production")
    return sid


def main(argv=None, service=None):
    p = argparse.ArgumentParser(prog="sheet_sync.verify", description="Read-only before/after integrity check.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="save all tabs (FORMULA + UNFORMATTED_VALUE) to a JSON file")
    s.add_argument("--out", required=True)
    s.add_argument("--allow-production", action="store_true")
    c = sub.add_parser("compare", help="compare a before-snapshot with an after-snapshot (taken now if --after is omitted)")
    c.add_argument("--before", required=True)
    c.add_argument("--after")
    c.add_argument("--week", type=int, required=True)
    c.add_argument("--expected", help="comma-separated cells expected to change on Week N, e.g. E2,L8")
    c.add_argument("--allow-production", action="store_true")
    args = p.parse_args(argv)

    try:
        if args.cmd == "compare" and not 1 <= args.week <= 14:
            raise VerifyError("--week must be 1..14")
        sid = _target(args)
        if service is None:
            service = readonly_service(config.service_account_info())
        if args.cmd == "snapshot":
            snap = take_snapshot(service, sid)
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(snap))
            print(f"snapshot: {len(snap['tabs'])} tabs -> {args.out}")
            return 0
        before = json.loads(Path(args.before).read_text())
        if args.after:
            after = json.loads(Path(args.after).read_text())
        else:
            after = take_snapshot(service, sid)
            out = Path(args.before).with_name(Path(args.before).stem + ".after.json")
            out.write_text(json.dumps(after))
            print(f"after-snapshot: {len(after['tabs'])} tabs -> {out}")
        expected = [x.strip() for x in args.expected.split(",") if x.strip()] if args.expected else None
        if expected and any(not re.match(r"^[A-Z]+\d+$", x) for x in expected):
            raise VerifyError("--expected must be cells like E2,L8")
        ok, lines = compare(before, after, args.week, expected)
        print("\n".join(lines))
        return 0 if ok else 1
    except (VerifyError, config.ConfigError, sheet.SheetError, OSError, ValueError) as e:
        print(f"ERROR: {config.redact(str(e))}", file=sys.stderr)
        return 1
    except Exception:
        import traceback

        print("ERROR: " + config.redact("unexpected error\n" + traceback.format_exc()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
