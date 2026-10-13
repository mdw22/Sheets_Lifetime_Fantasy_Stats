# Sheets Lifetime Fantasy Stats

Weekly sync of the latest finished ESPN fantasy football week into the `Week N` tab of the all-years Google Sheet. It writes input cells only (C, E, H, L-O in rows 2-15). Standings, lifetime totals and awards are sheet formulas and update on their own.

## Setup

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real values; .env is git-ignored
```

- **Google:** create a service account with the Sheets API enabled. Share the sheet (or a duplicate, for testing) with the service account's email as Editor. Set `GOOGLE_SERVICE_ACCOUNT_JSON` to the key file path (local) or the key JSON itself (CI).
- **ESPN (private league):** `ESPN_S2` and `SWID` are cookies from a logged-in browser session on fantasy.espn.com. Also set `ESPN_LEAGUE_ID` and `ESPN_SEASON_YEAR`.
- **Sheet:** `GOOGLE_SHEET_ID` defaults to the production sheet. Point it at a duplicate while testing.
- **Team map:** run `python -m sheet_sync --list-teams`, then fill in `team_map.yaml` (ESPN team id -> exact column A name). The sync refuses to run while placeholders remain. Keep the map to ID -> sheet name only: delete the `# owner:` comments before committing so no real names or usernames go into git.

## Usage

```sh
python -m sheet_sync              # dry run: latest completed week, prints a cell diff
python -m sheet_sync --week 4     # dry run for a specific week
python -m sheet_sync --commit     # write (saves a CSV backup to backups/ first)
python -m sheet_sync --commit --allow-production   # required to write to the production sheet
python -m sheet_sync --list-teams
pytest                            # offline tests

# Read-only integrity check around a write (production needs --allow-production here too)
python -m sheet_sync.verify snapshot --out backups/before_w4.json
python -m sheet_sync --week 4 --commit
python -m sheet_sync.verify compare --before backups/before_w4.json --week 4
```

`verify compare` takes the after-snapshot itself (or reads `--after`). It fails (exit 1) if anything changed on `Week N` outside C/E/H/L-O rows 2-15, if any other tab's formulas changed, or if a new error value (`#REF!`, `#N/A`, ...) appeared anywhere. `--expected E2,L8,...` also requires the changed cells to be exactly that list.

## Rules

- **Target week:** the latest week where every matchup has an ESPN `winner` (not `UNDECIDED`). A `--week` that is not final prints `NOTHING WRITTEN: week N not complete` and exits 0.
- **Regular season only:** weeks above 14 (a `--week 15`, or a latest complete week in the playoffs) print `NOTHING WRITTEN: ... regular season only (weeks 1-14); nothing to do` and exit 0 without touching the sheet.
- **Exit codes and banners:** every run that does not write ends with a `NOTHING WRITTEN: <reason>` line (also in the GitHub run summary). Every abort exits 1: bad header, unmapped team, more than 3 column C mismatches, formula in a target cell, ESPN or Google auth failure, quota error, missing credential. If the write itself fails after it starts, the banner is `WRITE FAILED: SHEET MAY BE PARTLY UPDATED` instead, with the backup path.
- **Projected (E):** the sum of the starters' ESPN per-player projections for that week. ESPN doesn't keep a team-level projection once the matchup ends.
- **Week MVP (L/M):** the top-scoring starter. Every slot except bench (20) and IR (21) counts as a starter.
- **Bench MVP (N/O):** the top-scoring bench (slot 20) player. Players in IR (slot 21) are excluded.
- **Ties:** most points wins; on equal points, the name that sorts first alphabetically (case-insensitive) wins, and the run prints a warning.
- **Opponent (C):** if a pre-filled opponent disagrees with ESPN's schedule, the run prints a `WARN` line and ESPN's value is written. If more than 3 teams mismatch in one week, the run aborts and writes nothing (likely a wrong team map or week). This applies to scheduled runs too.
- **Summary block:** every run ends with a summary: the week and whether it is complete, the number of cells that would change per column, and for column E the max and mean difference between the sheet and ESPN (blank sheet values counted separately). Warnings are listed there. Output never contains credentials, so it is safe to paste.
- **Production guard:** each run prints which sheet it reads (last 6 characters of the ID). `--commit` against the production sheet is refused unless `--allow-production` is passed.
- **Safety:**
  - Writes are refused outside C/E/H/L-O, rows 2-15, tabs `Week 1`..`Week 14`.
  - Writes are refused if the header row differs from the spec.
  - Writes are refused if a target cell holds a formula.
  - All writes go out as one RAW batch update containing only the changed cells.

## Automation

`.github/workflows/weekly_sheet_update.yml` runs Tuesday and Wednesday at 14:17 UTC (10:17 EDT; 09:17 EST after Nov 1). The minute is deliberately not :00: GitHub's scheduler is best-effort, and top-of-hour runs are the most likely to be delayed or dropped. If a scheduled run doesn't appear, dispatch the same run manually from the Actions tab (the sync is idempotent). Manual runs (`workflow_dispatch`) take `week` (optional) and `commit` (default false).

Writes are controlled by the repo variable **`SYNC_ENABLED`** (kill switch):

| `SYNC_ENABLED` | Scheduled run | Manual run, `commit=false` | Manual run, `commit=true` |
|---|---|---|---|
| exactly `true` | writes (`--commit --allow-production`) | dry run | writes (`--commit --allow-production`) |
| anything else (unset, `false`, `TRUE`) | dry run | dry run | refused, exit 1, "NOTHING WRITTEN" |

Settings to add in GitHub:

| Kind | Names |
|---|---|
| Secrets | `ESPN_S2`, `SWID`, `ESPN_LEAGUE_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON` |
| Variables | `ESPN_SEASON_YEAR`, `GOOGLE_SHEET_ID`, `SYNC_ENABLED` |

Each run uploads its backups as a workflow artifact.

This is a public repo: GitHub disables scheduled workflows in a public repo after 60 days with no repo activity. If scheduled runs stop, re-enable the workflow in the Actions tab (and pause it deliberately after the regular season).

## Operations

### Go-live checklist
1. Test copy: clean-slate rebuild of Weeks 1-4 passes and matches the golden snapshot (spec §12 check 6 prerequisites).
2. Real sheet: File -> Make a copy ("Phi Fantasy League Stats - backup 2026-10-08"); share the real sheet with the service account as Editor (sharing stays Restricted); fix `Lifetime!I52`.
3. Local `.env`: `GOOGLE_SHEET_ID` = real sheet. Production dry-runs, Weeks 1-5: planned cells as on the test copy (W1 15, W2 18, W3 18, W4 20), Week 5 "not complete", no C or H changes.
4. Copy the Week 1 F2:G15 formulas into Weeks 2-14 on the real sheet.
5. For Weeks 1-4: `verify snapshot`, `--week N --commit --allow-production`, zero-diff rerun, `verify compare --allow-production`.
6. Push the repo; add the GitHub secrets and variables (`GOOGLE_SHEET_ID` = real sheet, `SYNC_ENABLED` unset); run a manual dispatch dry-run and read its summary.
7. Set `SYNC_ENABLED` = `true`. Check each run summary and the sheet afterwards. (The first scheduled slot, Tue 2026-10-13 14:00 UTC, never fired; Week 5 was written by a manual dispatch and the crons moved to :17. First unattended write: Tue 2026-10-20 14:17 UTC, Week 6.)

### Rollback
- **Stop all writes now:** set the repo variable `SYNC_ENABLED` to anything but `true` (or delete it). No code change; the next scheduled run is a dry run.
- **Undo a write:** every commit run saves `backups/Week_N_<timestamp>.csv` first (in CI: the `sheet-backups-<run id>` artifact). It holds the tab's formula render; restore only the input cells (C, E, H, L-O rows 2-15) from it. Do not paste the whole CSV back, which would overwrite formulas with text. Alternatively use File -> Version history in Sheets.
- After restoring, run `python -m sheet_sync --week N` (dry run) to see what the sync would write again.

### Known cosmetic and hardening items (post-launch, after the production rollout)
Both are sheet-formula fixes, to be applied with an `ops` command (preview, then `--execute`; test copy first; production only with `--allow-production`). None of them is done by hand across the 14 tabs, and the sync writer never touches these formulas.
1. **Cosmetic (brought forward, designer Update 14):** Biggest Upset (B30) and Biggest Underachiever (B32) join the projected margin (G) and margin (K) into text without rounding, so floating-point noise can show (for example `-3.90000000000001 vs. -53.3`). Fix: `python -m ops fix-rounding` wraps those numbers in `ROUND(..., 2)` on all 14 Week tabs (28 cells). It previews by default, saves the 28 original formulas to `backups/fix_rounding_before_<timestamp>.json` before writing, and `--rollback <file>` restores them. Test copy first; production needs `--allow-production`.
2. **Hardening:** F (`VLOOKUP(C, A:E, 5, FALSE)`) and I (`VLOOKUP(C, A:H, 8, FALSE)`) look up whole columns, which include the award cells in rows 17-36. With C blank, F forms a circular dependency through Biggest Upset (`#REF!` on F2/G2/A30/B30); I is guarded by `IF(C="","",...)` and stayed blank. Fix: narrow the ranges to `$A$2:$E$15` and `$A$2:$H$15` on all 14 Week tabs.

### Pre-filled schedule check
`python -m ops check-schedule [--weeks 5-14]` (read-only) compares column C of each Week tab with ESPN's schedule through `team_map.yaml` and prints `n/14 match, b blank, m mismatch` per week, then every mismatch. Run it before a season's first scheduled write: the sync overwrites up to 3 opponent mismatches per week with a WARN and aborts on 4 or more.

### Refreshing ESPN cookies
`ESPN_S2` and `SWID` expire (typically after months, or on logout/password change). Symptom: the run fails loudly with `ESPN auth failure (HTTP 401)` and `NOTHING WRITTEN: aborted`; GitHub emails the repo owner on a failed run.
1. Log in at fantasy.espn.com in a browser.
2. DevTools -> Application (Storage) -> Cookies -> `https://fantasy.espn.com`: copy `espn_s2` and `SWID` (including the braces).
3. Update the GitHub secrets `ESPN_S2` and `SWID` (and your local `.env`).
4. Run the workflow manually with `commit=false` and check the summary.
