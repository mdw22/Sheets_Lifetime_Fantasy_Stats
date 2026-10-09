"""Offline fixtures: synthetic ESPN JSON and an in-memory Sheets service."""

import copy
import random
import re

import pytest
import yaml

from sheet_sync import sheet

N_TEAMS = 14
# (lineupSlotId, points, projected) per roster slot; bench = 20, IR = 21.
ROSTER = [(0, 20.0, 18.0), (2, 15.0, 14.0), (4, 12.5, 11.0), (23, 8.0, 9.5), (16, 5.0, 6.0),
          (20, 17.0, 10.0), (20, 3.0, 4.0), (21, 30.0, 0.0)]


def player_entry(name, slot, pts, proj, week):
    return {
        "lineupSlotId": slot,
        "playerPoolEntry": {"player": {"fullName": name, "stats": [
            {"scoringPeriodId": week, "statSourceId": 0, "statSplitTypeId": 1, "appliedTotal": pts},
            {"scoringPeriodId": week, "statSourceId": 1, "statSplitTypeId": 1, "appliedTotal": proj},
            {"scoringPeriodId": 0, "statSourceId": 0, "statSplitTypeId": 0, "appliedTotal": 999.0},
        ]}},
    }


def team_side(tid, week, bonus=0.0):
    entries = [player_entry(f"P{tid}-{i}", slot, pts + bonus, proj, week) for i, (slot, pts, proj) in enumerate(ROSTER)]
    total = sum(pts + bonus for slot, pts, _ in ROSTER if slot not in (20, 21))
    return {"teamId": tid, "totalPoints": total, "rosterForCurrentScoringPeriod": {"entries": entries}}


def week_json(week, winner="HOME"):
    sched = []
    for home in range(1, N_TEAMS + 1, 2):
        sched.append({"matchupPeriodId": week, "winner": winner,
                      "home": team_side(home, week, bonus=home / 10),
                      "away": team_side(home + 1, week, bonus=(home + 1) / 10)})
    return {"schedule": sched}


def schedule_json(complete_weeks, in_progress=None, last=14):
    sched = []
    for w in range(1, last + 1):
        winner = "HOME" if w in complete_weeks else "UNDECIDED"
        for home in range(1, N_TEAMS + 1, 2):
            sched.append({"matchupPeriodId": w, "winner": winner, "home": {"teamId": home}, "away": {"teamId": home + 1}})
    return {"schedule": sched}


class FakeEspn:
    def __init__(self, complete_weeks=(1, 2, 3, 4)):
        self.complete = set(complete_weeks)

    def fetch_schedule(self):
        return schedule_json(self.complete)

    def fetch_week(self, week):
        return week_json(week, "HOME" if week in self.complete else "UNDECIDED")

    def fetch_teams(self):
        return {"members": [{"id": "{m1}", "displayName": "owner1"}],
                "teams": [{"id": 1, "name": "Team One", "owners": ["{m1}"]}]}


def make_tab(names):
    vals = [list(sheet.HEADER)]
    forms = [list(sheet.HEADER)]
    for i, name in enumerate(names):
        r = i + 2
        row = [name, f"Mgr {name}"] + [""] * 13
        frow = list(row)
        for col in "DFGIJK":
            frow[sheet.COLUMNS.index(col)] = f"=LET(x, {col}{r}, x)"
        vals.append(row)
        forms.append(frow)
    return {"values": vals, "formulas": forms}


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


def _col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _parse_range(a1):
    """"'Tab'" or "'Tab'!F2:G15" / "'Tab'!C5" -> (tab, (r0, c0, r1, c1) or None); 0-based inclusive."""
    m = re.match(r"'([^']+)'(?:!([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?)?$", a1)
    tab = m.group(1)
    if not m.group(2):
        return tab, None
    c0, r0 = _col_index(m.group(2)), int(m.group(3)) - 1
    c1, r1 = (_col_index(m.group(4)), int(m.group(5)) - 1) if m.group(4) else (c0, r0)
    return tab, (r0, c0, r1, c1)


def _set(grid, r, c, v):
    while len(grid) <= r:
        grid.append([])
    row = grid[r]
    row.extend([""] * (c + 1 - len(row)))
    row[c] = v


class FakeValues:
    """In-memory Sheets values API. Formulas are not evaluated: a written value lands in both grids."""

    def __init__(self, tabs):
        self.tabs = tabs
        self.batch_calls = []
        self.clear_calls = []

    def _read(self, a1, render):
        tab, rect = _parse_range(a1)
        grid = self.tabs[tab]["formulas" if render == "FORMULA" else "values"]
        if rect is None:
            return copy.deepcopy(grid)
        r0, c0, r1, c1 = rect
        out = []
        for r in range(r0, r1 + 1):
            row = grid[r] if r < len(grid) else []
            vals = [row[c] if c < len(row) else "" for c in range(c0, c1 + 1)]
            while vals and vals[-1] == "":
                vals.pop()
            out.append(vals)
        while out and not out[-1]:
            out.pop()
        return out

    def get(self, spreadsheetId, range, valueRenderOption):
        return _Req(lambda: {"values": self._read(range, valueRenderOption)})

    def batchGet(self, spreadsheetId, ranges, valueRenderOption):
        return _Req(lambda: {"valueRanges": [{"values": self._read(r, valueRenderOption)} for r in ranges]})

    def batchUpdate(self, spreadsheetId, body):
        assert body["valueInputOption"] in ("RAW", "USER_ENTERED")
        self.batch_calls.append(body)

        def apply():
            n = 0
            for d in body["data"]:
                tab, (r0, c0, _, _) = _parse_range(d["range"])
                for i, row in enumerate(d["values"]):
                    for j, v in enumerate(row):
                        for key in ("values", "formulas"):
                            _set(self.tabs[tab][key], r0 + i, c0 + j, v)
                        n += 1
            return {"totalUpdatedCells": n}

        return _Req(apply)

    def batchClear(self, spreadsheetId, body):
        self.clear_calls.append(body)

        def apply():
            for a1 in body["ranges"]:
                tab, (r0, c0, r1, c1) = _parse_range(a1)
                for key in ("values", "formulas"):
                    grid = self.tabs[tab][key]
                    for r in range(r0, min(r1, len(grid) - 1) + 1):
                        for c in range(c0, min(c1, len(grid[r]) - 1) + 1):
                            grid[r][c] = ""
            return {"clearedRanges": body["ranges"]}

        return _Req(apply)


class FakeService:
    def __init__(self, tabs):
        self.fake_values = FakeValues(tabs)

    def spreadsheets(self):
        return self

    def get(self, spreadsheetId, fields=None):
        tabs = list(self.fake_values.tabs)
        return _Req(lambda: {"sheets": [{"properties": {"title": t}} for t in tabs]})

    def values(self):
        return self.fake_values


@pytest.fixture
def names():
    return [f"Sheet Team {i}" for i in range(1, N_TEAMS + 1)]


@pytest.fixture
def team_map_file(tmp_path, names):
    p = tmp_path / "team_map.yaml"
    p.write_text(yaml.safe_dump({"teams": {i: names[i - 1] for i in range(1, N_TEAMS + 1)}}))
    return p


@pytest.fixture
def service(names):
    shuffled = list(names)
    random.Random(7).shuffle(shuffled)  # row order must not matter
    tabs = {f"Week {w}": make_tab(shuffled) for w in range(1, 15)}
    standings = [["Team", "W", "L", "PF"]] + [[n, "=COUNTIF(x)", "=COUNTIF(y)", "=SUM(z)"] for n in names]
    tabs["2026"] = {"values": [["Team", "W", "L", "PF"]] + [[n, 0, 0, 0] for n in names], "formulas": standings}
    return FakeService(tabs)


@pytest.fixture
def sheet_client(service):
    return sheet.SheetClient("fake-id", service=service)
