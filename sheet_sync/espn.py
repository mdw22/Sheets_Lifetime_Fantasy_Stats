"""ESPN fantasy football fetch + parse, via ESPN's raw JSON endpoints.

Slot rules (ESPN lineupSlotId): 20 = bench, 21 = IR, anything else = starter.
Matchup completion: schedule[].winner is HOME / AWAY / TIE when final, UNDECIDED otherwise.
Projections for a finished week are per-player only (statSourceId=1); the team
projection is the sum of the starters' projections.
"""

import json
from dataclasses import dataclass, field

import requests

BASE_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{league_id}"
BENCH_SLOT = 20
IR_SLOT = 21
STAT_ACTUAL = 0
STAT_PROJECTED = 1
STAT_SPLIT_WEEK = 1


class EspnError(Exception):
    pass


@dataclass
class PlayerWeek:
    name: str
    slot_id: int
    points: float
    projected: float

    @property
    def is_starter(self):
        return self.slot_id not in (BENCH_SLOT, IR_SLOT)

    @property
    def is_bench(self):
        return self.slot_id == BENCH_SLOT


@dataclass
class TeamWeek:
    team_id: int
    opponent_id: int
    points: float
    players: list = field(default_factory=list)

    @property
    def starters(self):
        return [p for p in self.players if p.is_starter]

    @property
    def bench(self):
        return [p for p in self.players if p.is_bench]

    @property
    def projected(self):
        return round(sum(p.projected for p in self.starters), 2)


class EspnClient:
    def __init__(self, cfg, session=None):
        self.url = BASE_URL.format(year=cfg.season_year, league_id=cfg.league_id)
        self.session = session or requests.Session()
        self.session.cookies.set("espn_s2", cfg.espn_s2)
        self.session.cookies.set("SWID", cfg.swid)

    def _get(self, views, scoring_period=None, fantasy_filter=None):
        params = [("view", v) for v in views]
        if scoring_period is not None:
            params.append(("scoringPeriodId", scoring_period))
        headers = {"x-fantasy-filter": json.dumps(fantasy_filter)} if fantasy_filter else {}
        try:
            r = self.session.get(self.url, params=params, headers=headers, timeout=30)
        except requests.RequestException as e:
            # The exception text can quote request headers, including the auth cookies.
            raise EspnError(f"ESPN request failed ({type(e).__name__})")
        if r.status_code in (401, 403):
            raise EspnError(
                f"ESPN auth failure (HTTP {r.status_code}). ESPN_S2/SWID are missing, expired, or lack access to this private league."
            )
        if r.status_code == 404:
            raise EspnError("ESPN league not found (HTTP 404). Check ESPN_LEAGUE_ID and ESPN_SEASON_YEAR.")
        if r.status_code != 200:
            raise EspnError(f"ESPN returned HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            # ESPN serves an HTML login page instead of JSON when cookies are rejected.
            raise EspnError("ESPN returned non-JSON (likely a login page): ESPN auth failure.")

    def fetch_teams(self):
        return self._get(["mTeam"])

    def fetch_schedule(self):
        return self._get(["mMatchupScore"])

    def fetch_week(self, week):
        filt = {"schedule": {"filterMatchupPeriodIds": {"value": [week]}}}
        return self._get(["mMatchupScore", "mBoxscore"], scoring_period=week, fantasy_filter=filt)


def list_teams(teams_json):
    """[(team_id, espn_name, owner_names)] sorted by id."""
    members = {}
    for m in teams_json.get("members", []):
        full = f"{m.get('firstName', '')} {m.get('lastName', '')}".strip()
        display = m.get("displayName", "")
        members[m["id"]] = f"{full} ({display})" if full and display else full or display
    rows = []
    for t in teams_json.get("teams", []):
        name = t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}".strip()
        owners = ", ".join(members.get(o, o) for o in t.get("owners", []))
        rows.append((t["id"], name, owners))
    return sorted(rows)


def week_status(schedule_json, week):
    """'complete', 'incomplete', or 'missing' for a matchup period."""
    games = [m for m in schedule_json.get("schedule", []) if m.get("matchupPeriodId") == week]
    if not games:
        return "missing"
    if all(m.get("winner", "UNDECIDED") != "UNDECIDED" for m in games):
        return "complete"
    return "incomplete"


def latest_complete_week(schedule_json, max_week=None):
    """Latest matchup period with every game final; max_week=None means no cap (includes playoffs)."""
    if max_week is None:
        max_week = max((m.get("matchupPeriodId", 0) for m in schedule_json.get("schedule", [])), default=0)
    complete = [w for w in range(1, max_week + 1) if week_status(schedule_json, w) == "complete"]
    return max(complete) if complete else None


def _player_week(entry, week):
    player = entry["playerPoolEntry"]["player"]
    actual = projected = 0.0
    for s in player.get("stats", []):
        if s.get("scoringPeriodId") != week or s.get("statSplitTypeId") != STAT_SPLIT_WEEK:
            continue
        if s.get("statSourceId") == STAT_ACTUAL:
            actual = s.get("appliedTotal", 0.0)
        elif s.get("statSourceId") == STAT_PROJECTED:
            projected = s.get("appliedTotal", 0.0)
    return PlayerWeek(
        name=player.get("fullName", ""),
        slot_id=entry["lineupSlotId"],
        points=round(actual, 2),
        projected=round(projected, 2),
    )


def parse_week(week_json, week):
    """Return {team_id: TeamWeek} for the week. Raises if any matchup is not final."""
    teams = {}
    for m in week_json.get("schedule", []):
        if m.get("matchupPeriodId") != week:
            continue
        if m.get("winner", "UNDECIDED") == "UNDECIDED":
            raise EspnError(f"week {week} not complete")
        sides = [s for s in ("home", "away") if s in m]
        for side in sides:
            other = "away" if side == "home" else "home"
            d = m[side]
            roster = d.get("rosterForCurrentScoringPeriod", {}).get("entries", [])
            teams[d["teamId"]] = TeamWeek(
                team_id=d["teamId"],
                opponent_id=m[other]["teamId"] if other in m else None,
                points=round(d["totalPoints"], 2) if d.get("totalPoints") is not None else None,
                players=[_player_week(e, week) for e in roster],
            )
    return teams
