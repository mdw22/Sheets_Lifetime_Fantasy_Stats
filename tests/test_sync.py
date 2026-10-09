import argparse
import json

import pytest
import requests
import yaml

from sheet_sync import build, cli, config, espn, sheet, verify
from tests.conftest import FakeEspn, FakeService, make_tab, schedule_json, week_json


def args(team_map, tmp_path, **kw):
    base = dict(week=None, commit=False, allow_production=False, list_teams=False,
                team_map=str(team_map), backup_dir=str(tmp_path / "backups"))
    base.update(kw)
    return argparse.Namespace(**base)


# --- allowlist ---------------------------------------------------------------

@pytest.mark.parametrize("tab,row,col", [
    ("Week 4", 2, "D"), ("Week 4", 2, "J"), ("Week 4", 2, "A"), ("Week 4", 2, "P"),
    ("Week 4", 17, "C"), ("Week 4", 1, "C"), ("Week 4", 16, "H"),
    ("2026", 2, "C"), ("Lifetime", 2, "C"), ("Regular Season", 2, "C"), ("Week 15", 2, "C"), ("Week 0", 2, "C"),
])
def test_allowlist_rejects(tab, row, col):
    with pytest.raises(sheet.AllowlistError):
        sheet.check_allowed(tab, row, col)


def test_allowlist_accepts_inputs():
    for col in "CEHLMNO":
        sheet.check_allowed("Week 1", 2, col)
        sheet.check_allowed("Week 14", 15, col)


def test_writer_enforces_allowlist(sheet_client, service):
    with pytest.raises(sheet.AllowlistError):
        sheet_client.write_cells("Week 4", {(2, "C"): "x", (2, "J"): "W"})
    with pytest.raises(sheet.AllowlistError):
        sheet_client.write_cells("2026", {(2, "C"): "x"})
    assert service.fake_values.batch_calls == []


# --- header ------------------------------------------------------------------

def test_header_ok():
    sheet.validate_header(list(sheet.HEADER))


def test_altered_header_aborts(tmp_path, team_map_file, service, sheet_client):
    service.fake_values.tabs["Week 4"]["values"][0][9] = "Result"
    rc_args = args(team_map_file, tmp_path, week=4, commit=True)
    with pytest.raises(sheet.SheetError, match="Header"):
        cli.run(rc_args, FakeEspn(), sheet_client)
    assert service.fake_values.batch_calls == []


# --- team mapping ------------------------------------------------------------

def test_unknown_team_id_aborts(tmp_path, names, service, sheet_client):
    p = tmp_path / "map.yaml"
    p.write_text(yaml.safe_dump({"teams": {i: names[i - 1] for i in range(1, 14)}}))  # id 14 missing
    with pytest.raises(build.ValidationError, match=r"\[14\]"):
        cli.run(args(p, tmp_path, week=4, commit=True), FakeEspn(), sheet_client)
    assert service.fake_values.batch_calls == []


def test_name_not_in_column_a_aborts(tmp_path, names, service, sheet_client):
    p = tmp_path / "map.yaml"
    m = {i: names[i - 1] for i in range(1, 15)}
    m[3] = "Renamed Team"
    p.write_text(yaml.safe_dump({"teams": m}))
    with pytest.raises(build.ValidationError, match="Renamed Team"):
        cli.run(args(p, tmp_path, week=4, commit=True), FakeEspn(), sheet_client)
    assert service.fake_values.batch_calls == []


def test_placeholder_team_map_aborts(tmp_path):
    p = tmp_path / "map.yaml"
    p.write_text("teams:\n  1: PLACEHOLDER Team 1\n")
    with pytest.raises(build.ValidationError, match="placeholder"):
        build.load_team_map(p)


# --- value building ----------------------------------------------------------

def P(name, slot, pts):
    return espn.PlayerWeek(name=name, slot_id=slot, points=pts, projected=0)


def test_mvp_is_top_starter_and_bench_excludes_ir():
    teams = espn.parse_week(week_json(4), 4)
    t = teams[1]
    name, pts, _ = build.pick_mvp(t.starters)
    assert (name, pts) == ("P1-0", 20.1)
    bname, bpts, _ = build.pick_mvp(t.bench)
    assert (bname, bpts) == ("P1-5", 17.1)  # IR player (30.1) excluded
    assert t.projected == 58.5  # sum of starter projections only


def test_mvp_tie_is_alphabetical():
    players = [P("Zed", 0, 20.0), P("amy", 2, 20.0), P("Bob", 4, 19.0)]
    assert build.pick_mvp(players) == ("amy", 20.0, True)
    assert build.pick_mvp(list(reversed(players))) == ("amy", 20.0, True)


def test_empty_bench():
    assert build.pick_mvp([]) == ("", "", False)


def test_build_cells_values(names):
    teams = espn.parse_week(week_json(4), 4)
    column_a = list(reversed(names))
    cells, warnings = build.build_cells(teams, {i: names[i - 1] for i in range(1, 15)}, column_a, 4)
    row = column_a.index("Sheet Team 1") + 2
    assert cells[(row, "C")] == "Sheet Team 2"
    assert cells[(row, "H")] == pytest.approx(61.0)
    assert isinstance(cells[(row, "E")], float)
    assert {c for _, c in cells} == set("CEHLMNO")
    assert warnings == []


def test_negative_pf_aborts(names):
    teams = espn.parse_week(week_json(4), 4)
    teams[5].points = -1
    with pytest.raises(build.ValidationError, match="negative"):
        build.build_cells(teams, {i: names[i - 1] for i in range(1, 15)}, names, 4)


# --- end to end: commit, idempotency, incomplete week ------------------------

def test_commit_then_zero_diff(tmp_path, team_map_file, service, sheet_client, capsys):
    before = {t: [list(r) for r in v["formulas"]] for t, v in service.fake_values.tabs.items()}
    assert cli.run(args(team_map_file, tmp_path, commit=True), FakeEspn(), sheet_client) == 0
    out = capsys.readouterr().out
    assert "Target: Week 4" in out and "week 5 not complete" in out
    assert len(service.fake_values.batch_calls) == 1
    assert service.fake_values.batch_calls[0]["valueInputOption"] == "RAW"  # sync never USER_ENTERED
    assert list((tmp_path / "backups").glob("Week_4_*.csv"))

    # Only C, E, H, L-O in rows 2-15 of Week 4 changed; formulas intact; other tabs untouched.
    after = service.fake_values.tabs
    for tab, v in after.items():
        for r, row in enumerate(v["formulas"], start=1):
            for c, val in enumerate(row):
                col = sheet.COLUMNS[c]
                old = before[tab][r - 1][c] if c < len(before[tab][r - 1]) else ""
                if val != old:
                    assert tab == "Week 4" and col in "CEHLMNO" and 2 <= r <= 15

    assert cli.run(args(team_map_file, tmp_path, commit=True), FakeEspn(), sheet_client) == 0
    assert "No changes" in capsys.readouterr().out
    assert len(service.fake_values.batch_calls) == 1


def test_dry_run_writes_nothing(tmp_path, team_map_file, service, sheet_client):
    assert cli.run(args(team_map_file, tmp_path), FakeEspn(), sheet_client) == 0
    assert service.fake_values.batch_calls == []
    assert not (tmp_path / "backups").exists()


def test_incomplete_week_exits_zero(tmp_path, team_map_file, service, sheet_client, capsys):
    assert cli.run(args(team_map_file, tmp_path, week=5, commit=True), FakeEspn(), sheet_client) == 0
    assert "week 5 not complete" in capsys.readouterr().out
    assert service.fake_values.batch_calls == []


def test_parse_week_refuses_unfinished():
    with pytest.raises(espn.EspnError, match="not complete"):
        espn.parse_week(week_json(5, winner="UNDECIDED"), 5)


def test_target_week_detection():
    sched = FakeEspn(complete_weeks=(1, 2, 3, 4)).fetch_schedule()
    assert espn.latest_complete_week(sched, 14) == 4
    assert espn.week_status(sched, 5) == "incomplete"


def test_target_cell_holding_formula_aborts(tmp_path, team_map_file, service, sheet_client):
    service.fake_values.tabs["Week 4"]["formulas"][3][7] = "=SUM(1,2)"
    with pytest.raises(sheet.SheetError, match="formulas"):
        cli.run(args(team_map_file, tmp_path, week=4, commit=True), FakeEspn(), sheet_client)


def test_prefilled_opponent_mismatch_warns(tmp_path, team_map_file, service, sheet_client, capsys):
    grid = service.fake_values.tabs["Week 4"]["values"]
    grid[1][2] = "Someone Else"
    cli.run(args(team_map_file, tmp_path, week=4), FakeEspn(), sheet_client)
    assert "WARN: C2: sheet has opponent 'Someone Else'" in capsys.readouterr().out


def _prefill_wrong_opponents(service, n):
    grid = service.fake_values.tabs["Week 4"]["values"]
    for r in range(1, n + 1):
        grid[r][2] = "Someone Else"


def test_three_opponent_mismatches_warn_and_write(tmp_path, team_map_file, service, sheet_client, capsys):
    _prefill_wrong_opponents(service, 3)
    assert cli.run(args(team_map_file, tmp_path, week=4, commit=True), FakeEspn(), sheet_client) == 0
    assert capsys.readouterr().out.count("WARN: C") == 3
    assert len(service.fake_values.batch_calls) == 1
    assert service.fake_values.tabs["Week 4"]["values"][1][2] != "Someone Else"


def test_four_opponent_mismatches_abort(tmp_path, team_map_file, service, sheet_client):
    _prefill_wrong_opponents(service, 4)
    with pytest.raises(sheet.SheetError, match="4 opponent"):
        cli.run(args(team_map_file, tmp_path, week=4, commit=True), FakeEspn(), sheet_client)
    assert service.fake_values.batch_calls == []
    assert not (tmp_path / "backups").exists()


# --- credentials -------------------------------------------------------------

def test_missing_credential_fails_loudly(monkeypatch):
    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    for k in ("ESPN_LEAGUE_ID", "ESPN_SEASON_YEAR", "ESPN_S2", "SWID", "GOOGLE_SERVICE_ACCOUNT_JSON"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(config.ConfigError, match="ESPN_LEAGUE_ID"):
        config.espn_config()
    with pytest.raises(config.ConfigError, match="GOOGLE_SERVICE_ACCOUNT_JSON"):
        config.service_account_info()


def test_placeholder_credential_rejected(monkeypatch):
    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    monkeypatch.setenv("ESPN_LEAGUE_ID", "123")
    monkeypatch.setenv("ESPN_SEASON_YEAR", "2026")
    monkeypatch.setenv("ESPN_S2", "PLACEHOLDER_ESPN_S2")
    monkeypatch.setenv("SWID", "{x}")
    with pytest.raises(config.ConfigError, match="ESPN_S2"):
        config.espn_config()


def test_main_returns_1_on_error(monkeypatch, capsys):
    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    monkeypatch.delenv("ESPN_LEAGUE_ID", raising=False)
    assert cli.main([]) == 1
    assert "ERROR: Missing required environment variable: ESPN_LEAGUE_ID" in capsys.readouterr().err


# --- dry-run summary block ---------------------------------------------------

def test_summary_block_counts_and_e_stats(tmp_path, team_map_file, service, sheet_client, capsys):
    vals = service.fake_values.tabs["Week 4"]["values"]
    a_to_row = {vals[r][0]: r for r in range(1, 15)}
    vals[a_to_row["Sheet Team 1"]][4] = 60.5   # ESPN computes 58.5 -> diff 2.0
    vals[a_to_row["Sheet Team 2"]][4] = 58.5   # exact match
    assert cli.run(args(team_map_file, tmp_path, week=4), FakeEspn(), sheet_client) == 0
    out = capsys.readouterr().out
    assert "=== Summary ===" in out and "Week 4: complete (dry-run)" in out
    assert "Cells that would change: 97 (C=14, E=13, H=14, L=14, M=14, N=14, O=14)" in out
    assert "max |diff| 2.00, mean |diff| 1.00 over 2 team(s); blank/non-numeric current values: 12" in out


def test_summary_block_incomplete_week(tmp_path, team_map_file, sheet_client, capsys):
    cli.run(args(team_map_file, tmp_path, week=5), FakeEspn(), sheet_client)
    out = capsys.readouterr().out
    assert "Week 5: not complete (dry-run)" in out and "Cells that would change: 0" in out


# --- production guard --------------------------------------------------------

def test_commit_to_production_refused_without_flag(tmp_path, team_map_file, service, capsys):
    prod = sheet.SheetClient(config.DEFAULT_SHEET_ID, service=service)
    with pytest.raises(sheet.SheetError, match="--allow-production"):
        cli.run(args(team_map_file, tmp_path, week=4, commit=True), FakeEspn(), prod)
    assert service.fake_values.batch_calls == []
    assert "Sheet: ...{} (PRODUCTION)".format(config.DEFAULT_SHEET_ID[-6:]) in capsys.readouterr().out


def test_commit_to_production_with_flag(tmp_path, team_map_file, service):
    prod = sheet.SheetClient(config.DEFAULT_SHEET_ID, service=service)
    assert cli.run(args(team_map_file, tmp_path, week=4, commit=True, allow_production=True), FakeEspn(), prod) == 0
    assert len(service.fake_values.batch_calls) == 1


def test_dry_run_banner_test_copy(tmp_path, team_map_file, sheet_client, capsys):
    cli.run(args(team_map_file, tmp_path, week=4), FakeEspn(), sheet_client)
    assert "Sheet: ...ake-id (test copy)" in capsys.readouterr().out


# --- list-teams --------------------------------------------------------------

def test_list_teams_prints_yaml(tmp_path, capsys):
    teams_json = {"members": [{"id": "{m1}", "displayName": "dude42", "firstName": "Ann", "lastName": "Lee"}],
                  "teams": [{"id": 7, "name": "Team \"Q\"", "owners": ["{m1}"]}]}
    for line in cli.list_teams_yaml(espn.list_teams(teams_json)):
        print(line)
    out = capsys.readouterr().out
    assert "teams:" in out
    snippet = yaml.safe_load("\n".join(l for l in out.splitlines() if not l.startswith("#")))
    assert snippet == {"teams": {7: 'Team "Q"'}}
    assert "# owner: Ann Lee (dude42)" in out


# --- secrets never reach output ----------------------------------------------

FAKE_S2 = "FAKES2SECRETVALUE0123456789abcdef"
FAKE_SWID = "{FAKE1111-2222-3333-4444-SWIDSECRET99}"
FAKE_KEY = "FAKEPRIVATEKEYMATERIALxyz987654321"


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


@pytest.fixture
def secret_env(monkeypatch, tmp_path, names, service):
    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    key = {"type": "service_account", "private_key": f"-----BEGIN PRIVATE KEY-----\n{FAKE_KEY}\n-----END PRIVATE KEY-----\n",
           "private_key_id": "FAKEKEYID00112233", "client_email": "bot@example.iam.gserviceaccount.com"}
    monkeypatch.setenv("ESPN_S2", FAKE_S2)
    monkeypatch.setenv("SWID", FAKE_SWID)
    monkeypatch.setenv("ESPN_LEAGUE_ID", "123")
    monkeypatch.setenv("ESPN_SEASON_YEAR", "2026")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "test-copy-id-abc123")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(key))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    real = sheet.SheetClient
    monkeypatch.setattr(sheet, "SheetClient", lambda sid, info: real(sid, service=service))
    return summary


def _espn_ok(self, url, params=None, headers=None, timeout=None):
    views = [v for k, v in params if k == "view"]
    if "mBoxscore" in views:
        return _Resp(200, week_json(4))
    return _Resp(200, schedule_json({1, 2, 3, 4}))


def _assert_clean(capsys, summary):
    out, err = capsys.readouterr()
    text = out + err + (summary.read_text() if summary.exists() else "")
    for secret in (FAKE_S2, FAKE_SWID, FAKE_SWID.strip("{}"), FAKE_KEY, "FAKEKEYID00112233"):
        assert secret not in text
    return out, err


def test_no_secrets_in_normal_dry_run(secret_env, monkeypatch, team_map_file, capsys):
    monkeypatch.setattr(requests.Session, "get", _espn_ok)
    assert cli.main(["--week", "4", "--team-map", str(team_map_file)]) == 0
    out, _ = _assert_clean(capsys, secret_env)
    assert "=== Summary ===" in out


def test_no_secrets_on_espn_auth_failure(secret_env, monkeypatch, team_map_file, capsys):
    monkeypatch.setattr(requests.Session, "get", lambda *a, **k: _Resp(401, {"cookie": FAKE_S2}))
    assert cli.main(["--week", "4", "--team-map", str(team_map_file)]) == 1
    _, err = _assert_clean(capsys, secret_env)
    assert "ESPN auth failure" in err


def test_no_secrets_when_request_exception_quotes_headers(secret_env, monkeypatch, team_map_file, capsys):
    def boom(*a, **k):
        raise requests.exceptions.InvalidHeader(f"Invalid header value b'espn_s2={FAKE_S2}; SWID={FAKE_SWID}'")
    monkeypatch.setattr(requests.Session, "get", boom)
    assert cli.main(["--week", "4", "--team-map", str(team_map_file)]) == 1
    _assert_clean(capsys, secret_env)


def test_no_secrets_in_unexpected_traceback(secret_env, monkeypatch, team_map_file, capsys):
    def boom(*a, **k):
        raise RuntimeError(f"weird failure with {FAKE_S2} and {FAKE_KEY}")
    monkeypatch.setattr(requests.Session, "get", boom)
    assert cli.main(["--week", "4", "--team-map", str(team_map_file)]) == 1
    _, err = _assert_clean(capsys, secret_env)
    assert "unexpected error" in err and "[REDACTED]" in err


def test_no_secrets_on_bad_key_value(secret_env, monkeypatch, team_map_file, capsys):
    monkeypatch.setattr(requests.Session, "get", _espn_ok)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", f"not-json-{FAKE_KEY}")
    assert cli.main(["--week", "4", "--team-map", str(team_map_file)]) == 1
    _, err = _assert_clean(capsys, secret_env)
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in err


# --- regular season only -----------------------------------------------------

def test_week_above_14_exits_zero_without_sheet_access(tmp_path, team_map_file, service, capsys):
    class NoSheet:
        spreadsheet_id = "fake-id"

        def __getattr__(self, name):
            raise AssertionError("sheet must not be touched")

    assert cli.run(args(team_map_file, tmp_path, week=15, commit=True), FakeEspn(), NoSheet()) == 0
    assert "NOTHING WRITTEN: week 15: regular season only (weeks 1-14); nothing to do" in capsys.readouterr().out


def test_auto_target_in_playoffs_exits_zero(tmp_path, team_map_file, service, sheet_client, capsys):
    class PlayoffEspn(FakeEspn):
        def fetch_schedule(self):
            return schedule_json(set(range(1, 16)), last=17)

    assert cli.run(args(team_map_file, tmp_path, commit=True), PlayoffEspn(range(1, 16)), sheet_client) == 0
    assert "latest complete week is 15: regular season only" in capsys.readouterr().out
    assert service.fake_values.batch_calls == []


def test_week_zero_is_an_error(tmp_path, team_map_file, sheet_client):
    with pytest.raises(sheet.AllowlistError):
        cli.run(args(team_map_file, tmp_path, week=0), FakeEspn(), sheet_client)


# --- banners and exit codes --------------------------------------------------

def test_incomplete_week_banner_in_summary(tmp_path, team_map_file, sheet_client, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert cli.run(args(team_map_file, tmp_path, week=5, commit=True), FakeEspn(), sheet_client) == 0
    assert "**NOTHING WRITTEN: week 5 not complete**" in summary.read_text()


def test_dry_run_and_noop_banners(tmp_path, team_map_file, sheet_client, capsys):
    cli.run(args(team_map_file, tmp_path, week=4), FakeEspn(), sheet_client)
    assert "NOTHING WRITTEN: dry run" in capsys.readouterr().out
    cli.run(args(team_map_file, tmp_path, week=4, commit=True), FakeEspn(), sheet_client)
    assert "WROTE 98 cell(s) to Week 4" in capsys.readouterr().out
    cli.run(args(team_map_file, tmp_path, week=4, commit=True), FakeEspn(), sheet_client)
    assert "NOTHING WRITTEN: Week 4 already matches ESPN" in capsys.readouterr().out


def _main_with_fakes(monkeypatch, tmp_path, service, espn_client, argv):
    monkeypatch.setattr(espn, "EspnClient", lambda cfg: espn_client)
    monkeypatch.setattr(config, "espn_config", lambda: None)
    monkeypatch.setattr(config, "sheet_id", lambda: "test-copy-id")
    monkeypatch.setattr(config, "service_account_info", lambda: {})
    real = sheet.SheetClient
    monkeypatch.setattr(sheet, "SheetClient", lambda sid, info: real(sid, service=service))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    return cli.main(argv + ["--backup-dir", str(tmp_path / "b")]), summary


@pytest.mark.parametrize("tamper,needle", [
    ("header", "Header row"),
    ("unmapped", "not found in column A"),
    ("mismatch", "opponent (column C) mismatches"),
])
def test_every_abort_exits_nonzero_with_banner(tamper, needle, monkeypatch, tmp_path, names, service, capsys):
    tab = service.fake_values.tabs["Week 4"]["values"]
    p = tmp_path / "map.yaml"
    m = {i: names[i - 1] for i in range(1, 15)}
    if tamper == "header":
        tab[0][9] = "Result"
    elif tamper == "unmapped":
        m[2] = "Nope"
    else:
        for r in range(1, 5):
            tab[r][2] = "Someone Else"
    p.write_text(yaml.safe_dump({"teams": m}))
    rc, summary = _main_with_fakes(monkeypatch, tmp_path, service, FakeEspn(), ["--week", "4", "--commit", "--team-map", str(p)])
    err = capsys.readouterr().err
    assert rc == 1 and needle in err and "NOTHING WRITTEN: aborted" in err
    assert "**NOTHING WRITTEN: aborted**" in summary.read_text()
    assert service.fake_values.batch_calls == []


def test_espn_auth_failure_exits_nonzero(monkeypatch, tmp_path, team_map_file, service, capsys):
    class AuthFail(FakeEspn):
        def fetch_schedule(self):
            raise espn.EspnError("ESPN auth failure (HTTP 401).")

    rc, _ = _main_with_fakes(monkeypatch, tmp_path, service, AuthFail(), ["--week", "4", "--team-map", str(team_map_file)])
    assert rc == 1 and "ESPN auth failure" in capsys.readouterr().err


def test_write_failure_banner(monkeypatch, tmp_path, team_map_file, service, capsys):
    def broken(*a, **k):
        raise sheet.SheetError("Google Sheets API quota exceeded (HTTP 429).")

    monkeypatch.setattr(sheet.SheetClient, "write_cells", broken)
    rc, summary = _main_with_fakes(monkeypatch, tmp_path, service, FakeEspn(), ["--week", "4", "--commit", "--team-map", str(team_map_file)])
    err = capsys.readouterr().err
    assert rc == 1 and "WRITE FAILED: SHEET MAY BE PARTLY UPDATED" in err and "NOTHING WRITTEN" not in err


# --- verify tool -------------------------------------------------------------

def _verify(monkeypatch, service, argv, sheet_id="test-copy-id"):
    monkeypatch.setattr(config, "sheet_id", lambda: sheet_id)
    return verify.main(argv, service=service)


def _commit_week4(tmp_path, team_map_file, service):
    client = sheet.SheetClient("fake-id", service=service)
    assert cli.run(args(team_map_file, tmp_path, week=4, commit=True), FakeEspn(), client) == 0


def test_verify_passes_after_clean_commit(monkeypatch, tmp_path, team_map_file, service, capsys):
    before = tmp_path / "before.json"
    assert _verify(monkeypatch, service, ["snapshot", "--out", str(before)]) == 0
    _commit_week4(tmp_path, team_map_file, service)
    assert _verify(monkeypatch, service, ["compare", "--before", str(before), "--week", "4"]) == 0
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out and "(test copy) [read-only]" in out
    assert (tmp_path / "before.after.json").exists()


def test_verify_expected_cells(monkeypatch, tmp_path, team_map_file, service, capsys):
    before = tmp_path / "before.json"
    _verify(monkeypatch, service, ["snapshot", "--out", str(before)])
    _commit_week4(tmp_path, team_map_file, service)
    assert _verify(monkeypatch, service, ["compare", "--before", str(before), "--week", "4", "--expected", "E2,E3"]) == 1
    assert "FAIL vs expected" in capsys.readouterr().out


@pytest.mark.parametrize("tamper", ["formula_col", "award_row", "other_tab", "new_error"])
def test_verify_catches_tampering(tamper, monkeypatch, tmp_path, service, capsys):
    before = tmp_path / "before.json"
    _verify(monkeypatch, service, ["snapshot", "--out", str(before)])
    tabs = service.fake_values.tabs
    if tamper == "formula_col":
        tabs["Week 4"]["formulas"][1][3] = "oops"
    elif tamper == "award_row":
        tabs["Week 4"]["formulas"].extend([[]] * 17 + [["=MAX(H2:H15)"]])
    elif tamper == "other_tab":
        tabs["2026"]["formulas"][1][1] = "=COUNTIF(changed)"
    else:
        tabs["2026"]["values"][1][1] = "#REF!"
    assert _verify(monkeypatch, service, ["compare", "--before", str(before), "--week", "4"]) == 1
    assert "RESULT: FAIL" in capsys.readouterr().out


def test_verify_refuses_production_without_flag(monkeypatch, tmp_path, service, capsys):
    out = tmp_path / "s.json"
    assert _verify(monkeypatch, service, ["snapshot", "--out", str(out)], sheet_id=config.DEFAULT_SHEET_ID) == 1
    assert "--allow-production" in capsys.readouterr().err and not out.exists()
    assert _verify(monkeypatch, service, ["snapshot", "--out", str(out), "--allow-production"],
                   sheet_id=config.DEFAULT_SHEET_ID) == 0
    assert "(PRODUCTION) [read-only]" in capsys.readouterr().out


def test_verify_never_writes(monkeypatch, tmp_path, team_map_file, service):
    before = tmp_path / "before.json"
    _verify(monkeypatch, service, ["snapshot", "--out", str(before)])
    _verify(monkeypatch, service, ["compare", "--before", str(before), "--week", "4"])
    assert service.fake_values.batch_calls == []
