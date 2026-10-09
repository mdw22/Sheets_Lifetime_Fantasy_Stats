# Spec: Weekly ESPN -> Google Sheet Sync

Status: v1 design. Owner: Michael. Designer: Claude (chat). Builder: Claude Code.
Repo: Sheets_Lifetime_Fantasy_Stats (standalone, separate from fantasy-week-roundup).

## 1. Goal
Once a week, read the last completed week's results from our private ESPN fantasy football league and write them into the correct `Week N` tab of our all-years Google Sheet. Touch input cells only. Every standing, lifetime total and award in the sheet is a formula that updates by itself once the inputs land.

## 2. Target sheet
- Spreadsheet ID: `1kE1qrD85nju4qVYHv0erDRDko0seQL6BRCh-rH5SRfU` (config, not a secret)
- Created in Google Sheets. It uses Google-only functions (LET, FILTER, ARRAY). NEVER overwrite or rewrite a formula cell.
- Tabs: `Lifetime`, `2026`, `Week 1` .. `Week 14`, `Regular Season`.
- Only `Week N` tabs are written to in v1.

## 3. Week tab layout (rows 2-15 = one row per team, 14 teams)
Header row 1 must be exactly:
A Team Name | B Team Manager | C Opponent Name | D Opponent Manager | E Projected | F Opponent Projected | G Projected Margin | H PF | I PA | J W/L/T | K Margin | L Week MVP | M MVP Points | N Bench MVP | O Bench MVP Points

WRITE (inputs): C, E, H, L, M, N, O
NEVER WRITE (formulas): D, F, G, I, J, K, and everything in rows 17+ (award blocks) and column P onward.
Columns A and B are fixed reference data. Read them, never change them.

Field definitions:
- C: opponent's sheet team name (from team_map.yaml), plain text.
- E: this team's ESPN projected points for the week.
- H: final points scored (PF).
- L/M: highest-scoring player in the team's STARTING lineup (name, points).
- N/O: highest-scoring player on the team's BENCH (name, points). Exclude IR/out-slot players only if ESPN flags them as such; report the rule used.
- Numbers are written as numbers (not text), 2 decimals max.

## 4. Team identity
- Match teams by ESPN team ID, never by name. Names change year to year and casing is inconsistent.
- `team_map.yaml`: ESPN team ID -> exact sheet name in column A. Placeholder IDs until real ones are supplied. Provide a `--list-teams` command that prints ESPN IDs + current ESPN names so the map can be filled in.
- Locate each team's row by reading column A of the target tab. Do not assume row order.
- If an ESPN team has no map entry, or a mapped name is not found in column A: abort, write nothing, say which one.

## 5. Behavior
1. Determine target week = most recent week where ALL matchups are final. Do not rely on ESPN's "current week" alone: on the day this runs it may already have advanced, or Monday games may still be live. `--week N` overrides.
2. If the target week has any unfinished game, exit cleanly with "week N not complete", write nothing, exit code 0.
3. Pull per matchup: both teams, projected points, final points, starting lineup points, bench points.
4. Build the full set of intended cell values in memory.
5. Validate: header row matches section 3; 14 teams found; every team appears once as a team and once as an opponent; no negative or missing PF.
6. Diff against current sheet values and print a table (cell, old, new).
7. Dry-run (default) stops here. `--commit` performs the write.
8. Writes go through one batch update, values only, USER_ENTERED off (raw numbers/text).
9. Before any commit: save a timestamped CSV of the tab's current values into `backups/` (git-ignored).
10. Idempotent: re-running the same week overwrites the same cells and produces zero diff the second time.

## 6. Open items Claude Code must verify first and report back (before building around them)
a. Does ESPN return PROJECTED points for a finished week? If not, propose a fallback (e.g. a Thursday "capture projections" run writing only column E for the upcoming week).
b. How ESPN marks bench vs starter vs IR slots in box scores; how ties are represented.
c. Which Python ESPN library to use (e.g. espn-api) and whether it supports the current season; fall back to ESPN's raw JSON endpoints if not.
d. Whether 2026 column C (opponent) is already pre-filled in future weeks. If it is, compare ESPN's schedule to it and WARN on any mismatch rather than silently overwriting.

## 7. Auth and config
- Google: service account. Share the sheet with the service account's email as Editor. JSON key supplied via env var `GOOGLE_SERVICE_ACCOUNT_JSON` (GitHub secret in CI; git-ignored local file for dev). Scope: Sheets only.
- ESPN: `ESPN_S2`, `SWID`, `ESPN_LEAGUE_ID`, `ESPN_SEASON_YEAR`. League is private.
- Other: `GOOGLE_SHEET_ID`.
- Build with placeholders first. Real credentials get plugged in afterward for testing. Nothing secret is ever committed.

## 8. Safety rules
- Dry-run is the default. Real writes require `--commit`.
- Hard allowlist: the writer may only target columns C, E, H, L-O in rows 2-15 of tabs named `Week 1`..`Week 14`. Anything else raises an error, even if requested.
- Refuse to write if the tab's header row differs from section 3.
- Never delete, insert or reorder rows/columns. Never change formatting.
- Loud failure with a clear message for: bad header, unknown team ID, missing credential, ESPN auth failure, API quota error.

## 9. Automation (GitHub Actions)
- Workflow: `.github/workflows/weekly_sheet_update.yml`
- Schedule: Tuesday 10:00 America/New_York (write in UTC, note DST drift in a comment) so it runs after Monday Night Football and after ESPN stat corrections. Add a Wednesday re-run for late corrections (safe, idempotent).
- `workflow_dispatch` with inputs: `week` (optional) and `commit` (boolean, default false).
- Scheduled runs use `--commit`. Manual runs default to dry-run.
- Run summary: week, cells changed, warnings.

## 10. Repo scaffold
README.md, requirements.txt, .gitignore, team_map.yaml, config handling, ESPN fetch module, sheet read/write module, CLI entry point, tests, the workflow file.
.gitignore must include: CLAUDE.md, DESIGNER_REPORT.md, specs/, backups/, .env, any service-account *.json, __pycache__/.
Do not push to origin until Michael reviews.

## 11. Tests (no network; use fixtures)
- Row/column allowlist: attempts to target D, J, row 17+, other tabs all fail.
- Header validation: altered header aborts.
- Team mapping: unknown ID aborts; name not in column A aborts.
- Value building: MVP = top starter, bench MVP = top bench player; ties handled deterministically (documented rule).
- Idempotency: second run produces empty diff.
- Incomplete week: exits 0, writes nothing.

## 12. Acceptance checks
1. Dry-run for Week 4 (the latest finished week as of 2026-10-08) reproduces what is already in the sheet within rounding. Week 5 is in progress and must be reported as "not complete". Week 6 is empty.
2. A `--commit` run against a DUPLICATE of the sheet changes only cells in C, E, H, L-O of the target tab; the `2026` tab standings update on their own.
3. A second identical run gives a zero diff.
4. No other tab changes. No formula cell changes.
5. Bad header / unknown team / missing credential each fail loudly.
6. First real production commit only after checks 1-5 pass on the duplicate.

## 13. Out of scope for v1 (v2 ideas)
- Playoff/championship columns on `Lifetime` (K-M) and the end-of-season step that freezes the 2026 block into static values and adds the next year.
- Playoff weeks.
- Optional: feed this same data to fantasy-week-roundup so ESPN is fetched once.

## 14. Known data issue (manual fix by Michael, not the script)
- `Lifetime!I52` (2024 House Stark PF/G) reads 11.84; should be about 118.4.
