"""CLI entry point: python -m sheet_sync [--week N] [--commit] [--list-teams]."""

import argparse
import json
import os
import sys
import traceback

from . import build, config, espn, sheet

MAX_WEEK = 14


class WriteFailed(Exception):
    """The batch write was attempted and failed; the sheet may be partly updated."""


def _fmt(v):
    return "" if v is None or v == "" else repr(v)


def print_diff(changes):
    if not changes:
        print("No changes: sheet already matches ESPN.")
        return
    w = max(len(c[0]) for c in changes)
    print(f"{'cell'.ljust(w)}  old -> new")
    for cell, old, new in changes:
        print(f"{cell.ljust(w)}  {_fmt(old)} -> {_fmt(new)}")


def write_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")


def list_teams_yaml(rows):
    lines = [
        "# Paste under team_map.yaml, then replace each ESPN name with the exact",
        "# column A name from the sheet. The owner comment is there to help match.",
        "teams:",
    ]
    for tid, name, owners in rows:
        lines.append(f"  {tid}: {json.dumps(name)}  # owner: {owners or 'unknown'}")
    return lines


def summary_block(week, complete, changes=(), grid=None, cells=None, mode="dry-run"):
    lines = ["", "=== Summary ===", f"Week {week}: {'complete' if complete else 'not complete'} ({mode})"]
    if not complete:
        lines.append("Cells that would change: 0")
        return lines
    counts = {c: 0 for c in sorted(sheet.WRITABLE_COLUMNS)}
    for cell, _, _ in changes:
        counts[cell[0]] += 1
    lines.append(f"Cells that would change: {len(changes)} (" + ", ".join(f"{c}={n}" for c, n in counts.items()) + ")")
    e = sheet.e_column_stats(grid, cells)
    if e["compared"]:
        lines.append(
            f"Column E vs sheet: max |diff| {e['max']:.2f}, mean |diff| {e['mean']:.2f} "
            f"over {e['compared']} team(s); blank/non-numeric current values: {e['blank']}"
        )
    else:
        lines.append(f"Column E vs sheet: nothing to compare; blank/non-numeric current values: {e['blank']}")
    return lines


def nothing_written(reason):
    line = f"NOTHING WRITTEN: {reason}"
    print(line)
    write_summary([f"**{line}**"])


def run(args, espn_client=None, sheet_client=None):
    if espn_client is None:
        espn_client = espn.EspnClient(config.espn_config())

    if args.list_teams:
        for line in list_teams_yaml(espn.list_teams(espn_client.fetch_teams())):
            print(line)
        return 0

    sheet_id = sheet_client.spreadsheet_id if sheet_client is not None else config.sheet_id()
    is_production = sheet_id == config.DEFAULT_SHEET_ID
    print(f"Sheet: ...{sheet_id[-6:]} ({'PRODUCTION' if is_production else 'test copy'})")
    if args.commit and is_production and not args.allow_production:
        raise sheet.SheetError("Refusing --commit against the production sheet without --allow-production")

    mode = "commit" if args.commit else "dry-run"
    schedule = espn_client.fetch_schedule()
    regular_season_only = f"regular season only (weeks 1-{MAX_WEEK}); nothing to do"
    if args.week is not None:
        week = args.week
        if week < 1:
            raise sheet.AllowlistError("--week must be 1 or more")
        if week > MAX_WEEK:
            nothing_written(f"week {week}: {regular_season_only}")
            return 0
        if espn.week_status(schedule, week) != "complete":
            nothing_written(f"week {week} not complete")
            block = summary_block(week, False, mode=mode)
            print("\n".join(block))
            write_summary(block)
            return 0
    else:
        week = espn.latest_complete_week(schedule)
        if week is None:
            nothing_written("no completed week yet")
            return 0
        if week > MAX_WEEK:
            nothing_written(f"latest complete week is {week}: {regular_season_only}")
            return 0
        if week < MAX_WEEK and espn.week_status(schedule, week + 1) == "incomplete":
            print(f"week {week + 1} not complete")

    tab = f"Week {week}"
    print(f"Target: {tab}")

    teams = espn.parse_week(espn_client.fetch_week(week), week)
    team_map = build.load_team_map(args.team_map)

    if sheet_client is None:
        sheet_client = sheet.SheetClient(sheet_id, config.service_account_info())
    grid, formula_grid = sheet_client.read_week(tab)

    sheet.validate_header(grid[0] if grid else [])
    column_a = [sheet.cell_value(grid, r, "A") for r in sheet.WRITABLE_ROWS]
    cells, warnings = build.build_cells(teams, team_map, column_a, week)

    for (row, col) in cells:
        sheet.check_allowed(tab, row, col)
    formulas = sheet.formula_cells(formula_grid, cells)
    if formulas:
        raise sheet.SheetError(f"Refusing to write: target cell(s) contain formulas: {formulas}")

    mismatches = sheet.schedule_mismatches(grid, cells)
    if len(mismatches) > sheet.MAX_SCHEDULE_MISMATCHES:
        raise sheet.SheetError(
            f"{len(mismatches)} opponent (column C) mismatches with ESPN's schedule "
            f"(limit {sheet.MAX_SCHEDULE_MISMATCHES}); likely a wrong team map or week. Nothing written. "
            + "; ".join(mismatches)
        )
    warnings = mismatches + warnings
    changes = sheet.diff(grid, cells)

    print_diff(changes)

    block = summary_block(week, True, changes, grid, cells, mode)
    block += [f"WARN: {w}" for w in warnings]

    if not args.commit:
        print("\n".join(block))
        write_summary(block)
        nothing_written("dry run (re-run with --commit to write)")
        return 0
    if not changes:
        print("\n".join(block))
        write_summary(block)
        nothing_written(f"{tab} already matches ESPN")
        return 0
    backup = sheet.write_backup(tab, sheet_client.read_tab(tab), args.backup_dir)
    print(f"Backup saved: {backup}")
    try:
        updated = sheet_client.write_cells(tab, {(int(c[1:]), c[0]): new for c, _, new in changes})
    except Exception as e:
        raise WriteFailed(
            f"write to {tab} failed ({type(e).__name__}: {e}). The sheet may be partly updated: "
            f"compare with {backup} or the Sheets version history before re-running."
        )
    print("\n".join(block))
    write_summary(block)
    line = f"WROTE {updated} cell(s) to {tab}"
    print(line)
    write_summary([f"**{line}**"])
    return 0


def _fail(message, written=False):
    message = config.redact(message)
    banner = "WRITE FAILED: SHEET MAY BE PARTLY UPDATED" if written else "NOTHING WRITTEN: aborted"
    print(f"ERROR: {message}", file=sys.stderr)
    print(banner, file=sys.stderr)
    write_summary([f"**{banner}**", f"ERROR: {message}"])
    return 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="sheet_sync", description="Sync last completed ESPN week into the Google Sheet.")
    p.add_argument("--week", type=int, help="target week (default: latest week with all matchups final)")
    p.add_argument("--commit", action="store_true", help="perform the write (default is dry-run)")
    p.add_argument("--allow-production", action="store_true", help="permit --commit against the production sheet")
    p.add_argument("--list-teams", action="store_true", help="print ESPN team ids, names and owners as team_map.yaml lines")
    p.add_argument("--team-map", default="team_map.yaml")
    p.add_argument("--backup-dir", default="backups")
    args = p.parse_args(argv)
    try:
        return run(args)
    except WriteFailed as e:
        return _fail(str(e), written=True)
    except (config.ConfigError, espn.EspnError, build.ValidationError, sheet.SheetError, sheet.AllowlistError) as e:
        return _fail(str(e))
    except Exception:
        # Library exceptions can quote request headers (cookies); redact before printing.
        return _fail("unexpected error\n" + traceback.format_exc())


if __name__ == "__main__":
    sys.exit(main())
