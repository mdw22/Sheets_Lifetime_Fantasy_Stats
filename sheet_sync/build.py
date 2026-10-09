"""Turn parsed ESPN week data into intended sheet cell values, with validation.

MVP tie rule: highest points wins; on equal points, the alphabetically first
player name wins (case-insensitive), and a warning is emitted.
"""

from pathlib import Path

import yaml

EXPECTED_TEAMS = 14
FIRST_ROW = 2
LAST_ROW = FIRST_ROW + EXPECTED_TEAMS - 1


class ValidationError(Exception):
    pass


def load_team_map(path):
    data = yaml.safe_load(Path(path).read_text()) or {}
    teams = data.get("teams") or {}
    mapping = {}
    for espn_id, name in teams.items():
        if str(name).startswith("PLACEHOLDER"):
            raise ValidationError(
                f"{path} still has placeholder entries (ESPN id {espn_id}). Run --list-teams and fill it in."
            )
        mapping[int(espn_id)] = str(name)
    if len(set(mapping.values())) != len(mapping):
        raise ValidationError(f"{path} maps two ESPN ids to the same sheet name")
    return mapping


def pick_mvp(players):
    """Return (name, points, tied) for the top scorer, or ("", "", False) if no players."""
    if not players:
        return "", "", False
    ranked = sorted(players, key=lambda p: (-p.points, p.name.lower()))
    top = ranked[0]
    tied = len(ranked) > 1 and ranked[1].points == top.points
    return top.name, top.points, tied


def build_cells(teams, team_map, column_a, week):
    """
    teams: {espn_team_id: TeamWeek}; column_a: sheet names for rows 2..15 in order.
    Returns ({(row, col): value}, warnings).
    """
    warnings = []

    unknown = sorted(tid for tid in teams if tid not in team_map)
    if unknown:
        raise ValidationError(f"ESPN team id(s) {unknown} have no entry in team_map.yaml")

    rows = {}
    for offset, name in enumerate(column_a):
        name = (name or "").strip()
        if not name:
            continue
        if name in rows:
            raise ValidationError(f"Sheet name {name!r} appears twice in column A")
        rows[name] = FIRST_ROW + offset

    missing = [team_map[tid] for tid in sorted(teams) if team_map[tid] not in rows]
    if missing:
        raise ValidationError(f"Mapped team name(s) not found in column A of Week {week}: {missing}")

    if len(teams) != EXPECTED_TEAMS:
        raise ValidationError(f"Expected {EXPECTED_TEAMS} teams in ESPN week {week}, found {len(teams)}")

    opponents = [t.opponent_id for t in teams.values()]
    if sorted(o for o in opponents if o is not None) != sorted(teams) or None in opponents:
        raise ValidationError(f"Week {week}: every team must appear exactly once as a team and once as an opponent")

    cells = {}
    for tid, t in sorted(teams.items()):
        name = team_map[tid]
        if t.points is None or t.points < 0:
            raise ValidationError(f"Week {week}: {name} has missing or negative PF ({t.points})")

        starter_sum = round(sum(p.points for p in t.starters), 2)
        if abs(starter_sum - t.points) > 0.01:
            warnings.append(f"{name}: starters sum to {starter_sum} but ESPN PF is {t.points}")

        mvp, mvp_pts, mvp_tie = pick_mvp(t.starters)
        bench, bench_pts, bench_tie = pick_mvp(t.bench)
        if mvp_tie:
            warnings.append(f"{name}: tie for Week MVP at {mvp_pts}; chose {mvp} (alphabetical)")
        if bench_tie:
            warnings.append(f"{name}: tie for Bench MVP at {bench_pts}; chose {bench} (alphabetical)")

        row = rows[name]
        cells[(row, "C")] = team_map[t.opponent_id]
        cells[(row, "E")] = t.projected
        cells[(row, "H")] = t.points
        cells[(row, "L")] = mvp
        cells[(row, "M")] = mvp_pts
        cells[(row, "N")] = bench
        cells[(row, "O")] = bench_pts
    return cells, warnings
