from flask import Flask, jsonify, render_template_string, send_file, request
import requests
import json
import os
import time
import math
import itertools
import threading
from datetime import datetime, timezone
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

# ============================================================
# SETTINGS
# ============================================================

LEAGUE_ID = 1459721
HISTORY_FILE = "fpl_history.json"

# Saved league membership + promotion/relegation history
# (created automatically on first run)
LEAGUES_FILE = "fpl_leagues.json"

# OPTIONAL: choose the STARTING leagues using FPL entry IDs.
# Leave both lists empty to auto-lock from the current standings
# (top 5 = Championship, 6th-10th = Challenger) on first run.
# After the first run, fpl_leagues.json is the source of truth
# (it tracks promotions/relegations) - delete it to start again.
MANUAL_LEAGUES = {
    "championship": [],   # e.g. [1234567, 2345678, ...]
    "challenger": []
}

# ---- Promotion / relegation ----
# Every PERIOD_LENGTH gameweeks the bottom 2 of the Championship
# (4th & 5th) swap with the top 2 of the Challenger league (1st & 2nd).
PERIOD_LENGTH = 5

# First gameweek of the game. None = start from the next gameweek
# after the leagues are first locked in.
GAME_START_GW = None

# True  = league tables show points scored in the CURRENT period only
#         (resets after each promotion/relegation). Recommended.
# False = tables show full-season points; swaps are decided on
#         season points at the end of each period.
RESET_POINTS_EACH_PERIOD = True

# ---- Curry for period winners ----
# The manager in 1st place in each league when a period ends wins a curry.
# True  = curries are kept (a manager who wins twice shows 2 curries)
# False = only the winners of the most recent period show a curry
CURRIES_STACK = True


# ---- Curry table ----
# Who owes the period winner a curry?
# "last" = the manager in last place in that league for the period
# "all"  = everyone else in that league
CURRY_OWED_BY = "last"

# Show curries for the periods BEFORE the game started (GW1-5, 6-10...)
# using the leagues as they were first locked in
BACKDATE_CURRIES = True

# Gameweeks up to and including this one were played as ONE league
# (everybody in a single table). After it the game split into
# Championship (top 5) and Challenger (6th-10th).
SINGLE_LEAGUE_UNTIL_GW = 5

# ---- Hits tracker ----
# Every transfer hit (-4, -8 ...) taken this season is saved here.
HITS_FILE = "fpl_hits.json"

# Hits parade (stars v clowns on the transfers picture).
# Saved so finished gameweeks are only worked out once.
PARADE_FILE = "fpl_hits_parade.json"

# "What if nobody had ever made a transfer" table (GW1 team kept all season)
GW1_FILE = "fpl_gw1_team.json"

# A transfer that costs a hit is judged on: points in - points out - 4.
# Set to False to judge on the raw points only (in v out).
PARADE_DEDUCT_HIT = True

# True = only the transfers that actually cost -4 get a star/clown.
# False = free transfers get one too.
PARADE_HITS_ONLY = False

# Points left on the bench, worked out once per finished gameweek and saved
# here so old gameweeks never need to be looked up again.
BENCH_FILE = "fpl_bench.json"

# Triple Captain results (which player was captained and what they scored),
# saved once per finished gameweek.
CHIPS_FILE = "fpl_chips.json"

# ---- Chips ----
# Each chip comes in two sets: one for GW1-19 and one for GW20-38.
# First-set chips expire after the GW19 deadline.
CHIP_SET_SPLIT_GW = 19

# ---- Live scores ----
# False = scores only count FINISHED gameweeks, using FPL's own final
#         numbers, so they match the FPL website. The table, captains,
#         bench and chip scores all move on once a gameweek has ended.
# True  = also show the gameweek in progress, worked out live by this
#         app from player points. Handy during matches, but it can be a
#         point or two out until FPL finalises bonus points and auto-subs.
SHOW_LIVE_SCORES = True

# (name in the FPL data, short label, full name)
CHIP_LIST = [
    ("wildcard", "WC", "Wildcard"),
    ("freehit", "FH", "Free Hit"),
    ("bboost", "BB", "Bench Boost"),
    ("3xc", "TC", "Triple Captain")
]

FPL_API = "https://fantasy.premierleague.com/api"

HEADERS = {
    "User-Agent": "Mozilla/5.0 FPL League Dashboard"
}


# ============================================================
# DATA FUNCTIONS
# ============================================================

def fpl_get(endpoint):
    """Get JSON data from the FPL API."""
    url = f"{FPL_API}/{endpoint}"

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=20
    )

    response.raise_for_status()
    return response.json()


def last_complete_gameweek(data):
    """Last gameweek that is over.

    FPL only sets a gameweek's own "finished" flag some time after the
    last match. So for each gameweek after the last flagged one that has
    already started, if every one of its fixtures is finished, count it
    as complete straight away.
    """

    events = data.get("events", [])

    done = max(
        (e["id"] for e in events if e.get("finished")),
        default=0
    )

    now = datetime.now(timezone.utc)

    while True:

        candidate = next(
            (e for e in events if e["id"] == done + 1),
            None
        )

        if not candidate:
            break

        started = bool(candidate.get("is_current"))

        deadline = candidate.get("deadline_time")

        if deadline and not started:
            try:
                started = datetime.fromisoformat(
                    deadline.replace("Z", "+00:00")
                ) <= now
            except Exception:
                pass

        if not started:
            break

        try:
            fixtures = fpl_get(f"fixtures/?event={candidate['id']}")
        except Exception:
            break

        if fixtures and all(f.get("finished") for f in fixtures):
            done = candidate["id"]
        else:
            break

    return done


@app.route("/api/debug-scores")
def api_debug_scores():
    """Compares this app's scores with FPL's own numbers, per manager.

    Open /api/debug-scores for a table, or /api/debug-scores?format=json
    for the raw data.
    """

    try:
        league_name, standings = get_league()

        finished_gw = get_current_gameweek()
        gw = get_live_gameweek()

        state = get_league_state(standings, finished_gw)

        names = {row["entry"]: row for row in standings}

        ctx = get_live_context(gw)

        managers = []

        for entry in state["assignments"]:

            entry = int(entry)

            picks = get_manager_picks(entry, gw)
            history = get_manager_history(entry)

            hist_row = next(
                (g for g in history.get("current", [])
                 if g.get("event") == gw),
                None
            )

            official = picks.get("entry_history") or {}

            calc = score_live_team(picks, ctx)

            hit = official.get("event_transfers_cost") or 0

            managers.append({
                "manager": names.get(entry, {}).get("player_name"),
                "team": names.get(entry, {}).get("entry_name"),
                "this_app_before_hit": calc["gross"] if calc else None,
                "fpl_picks_before_hit": official.get("points"),
                "fpl_history_before_hit":
                    hist_row.get("points") if hist_row else None,
                "fpl_league_table_gw": names.get(entry, {}).get("event_total"),
                "hit": hit,
                "this_app_after_hit":
                    (calc["gross"] - hit) if calc else None,
                "active_chip": picks.get("active_chip"),
                "auto_subs_fpl_applied":
                    len(picks.get("automatic_subs", []))
            })

        result = {
            "last_finished_gameweek": finished_gw,
            "gameweek_shown": gw,
            "managers": managers
        }

        if request.args.get("format") == "json":
            return jsonify(result)

        def cell(value):
            return "-" if value is None else str(value)

        rows = ""

        for m in managers:

            rows += (
                "<tr>"
                f"<td>{m['manager']}<br><small>{m['team']}</small></td>"
                f"<td><b>{cell(m['this_app_after_hit'])}</b></td>"
                f"<td>{cell(m['this_app_before_hit'])}</td>"
                f"<td>{cell(m['fpl_history_before_hit'])}</td>"
                f"<td>{cell(m['fpl_picks_before_hit'])}</td>"
                f"<td>{cell(m['fpl_league_table_gw'])}</td>"
                f"<td>{m['hit']}</td>"
                f"<td>{cell(m['active_chip'])}</td>"
                f"<td>{m['auto_subs_fpl_applied']}</td>"
                "</tr>"
            )

        page = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FPL score check</title>
<style>
body {{ font-family: Arial, sans-serif; background: #061a14;
        color: #fff; padding: 20px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #1c4a38; padding: 8px; text-align: left;
          font-size: 14px; }}
th {{ background: #0d3024; color: #00ff87; }}
small {{ color: #9ab3a8; }}
p {{ color: #a9c5b9; max-width: 900px; }}
a {{ color: #00ff87; }}
</style></head><body>
<h2>FPL score check - gameweek {gw}</h2>
<p>Last gameweek FPL counts as finished: {finished_gw}.
Compare the first column with your FPL mini-league page. The other
columns are FPL's own numbers for the same gameweek (they can lag
behind while games are being played).</p>
<div style="overflow-x:auto"><table>
<tr>
<th>Manager</th>
<th>This app (after hit)</th>
<th>This app (before hit)</th>
<th>FPL history (before hit)</th>
<th>FPL picks (before hit)</th>
<th>FPL league table GW</th>
<th>Hit</th>
<th>Chip</th>
<th>FPL auto-subs</th>
</tr>
{rows}
</table></div>
<p><a href="/">Back to the dashboard</a></p>
</body></html>"""

        return page

    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/api/debug-gw")
def api_debug_gw():
    """Shows what FPL says about the gameweeks (to find why one is stuck)."""

    try:
        data = fpl_get("bootstrap-static/")
        events = data.get("events", [])

        last_flagged = max(
            (e["id"] for e in events if e.get("finished")),
            default=0
        )

        report = {
            "last_flagged_finished_by_fpl": last_flagged,
            "this_app_counts_as_complete": last_complete_gameweek(data),
            "show_live_scores": SHOW_LIVE_SCORES,
            "gameweeks": []
        }

        for e in events:
            if last_flagged - 1 <= e["id"] <= last_flagged + 2:
                info = {
                    "id": e["id"],
                    "finished": e.get("finished"),
                    "is_current": e.get("is_current"),
                    "is_next": e.get("is_next"),
                    "data_checked": e.get("data_checked"),
                    "deadline_time": e.get("deadline_time")
                }

                if e["id"] == last_flagged + 1:
                    fixtures = fpl_get(f"fixtures/?event={e['id']}")
                    info["fixtures_total"] = len(fixtures)
                    info["fixtures_not_finished"] = [
                        {
                            "id": f.get("id"),
                            "kickoff": f.get("kickoff_time"),
                            "started": f.get("started"),
                            "finished_provisional":
                                f.get("finished_provisional"),
                            "finished": f.get("finished")
                        }
                        for f in fixtures if not f.get("finished")
                    ]

                report["gameweeks"].append(info)

        return jsonify(report)

    except Exception as error:
        return jsonify({"error": str(error)}), 500


def get_current_gameweek():
    """Return the most recently finished Gameweek."""

    return last_complete_gameweek(fpl_get("bootstrap-static/"))


def get_live_gameweek():
    """Return the gameweek to show.

    With SHOW_LIVE_SCORES on, the dashboard switches to a new gameweek
    the moment its deadline has passed (points start at 0 and build up
    as the games are played). Otherwise it shows the last finished one.
    """

    data = fpl_get("bootstrap-static/")

    last_finished = last_complete_gameweek(data)

    if not SHOW_LIVE_SCORES:
        return last_finished

    now = datetime.now(timezone.utc)

    latest = last_finished

    for event in data.get("events", []):

        started = bool(event.get("is_current"))

        deadline = event.get("deadline_time")

        if deadline and not started:
            try:
                started = datetime.fromisoformat(
                    deadline.replace("Z", "+00:00")
                ) <= now
            except Exception:
                pass

        if started and event["id"] > latest:
            latest = event["id"]

    return latest


def get_league():
    """Get current league standings."""

    endpoint = f"leagues-classic/{LEAGUE_ID}/standings/?page_standings=1"

    data = fpl_get(endpoint)

    league_name = data.get("league", {}).get(
        "name",
        "FPL Mini League"
    )

    results = data.get("standings", {}).get("results", [])

    # In case the league has more than 50 players
    if data.get("standings", {}).get("has_next"):
        page = 2

        while True:
            endpoint = (
                f"leagues-classic/{LEAGUE_ID}"
                f"/standings/?page_standings={page}"
            )

            page_data = fpl_get(endpoint)

            page_results = (
                page_data
                .get("standings", {})
                .get("results", [])
            )

            results.extend(page_results)

            if not page_data.get("standings", {}).get("has_next"):
                break

            page += 1

    return league_name, results


def get_manager_history(entry_id):
    """Get a manager's Gameweek history."""

    try:
        return fpl_get(f"entry/{entry_id}/history/")
    except Exception:
        return {"current": []}


def get_manager_picks(entry_id, gameweek):
    """Get a manager's squad picks for a Gameweek."""

    try:
        return fpl_get(f"entry/{entry_id}/event/{gameweek}/picks/")
    except Exception:
        return {"picks": []}


def get_player_names():
    """Map player element IDs to their short names."""

    data = fpl_get("bootstrap-static/")

    return {
        element["id"]: element.get("web_name", "Unknown")
        for element in data.get("elements", [])
    }


def get_live_points(gameweek):
    """Map player element IDs to their points for a Gameweek."""

    try:
        data = fpl_get(f"event/{gameweek}/live/")

        return {
            element["id"]: element.get("stats", {}).get("total_points", 0)
            for element in data.get("elements", [])
        }
    except Exception:
        return {}


def get_live_context(gameweek):
    """Everything needed to score a team live, the way the FPL site does.

    stats = each player's points and minutes so far
    pos   = each player's position (1 GK, 2 DEF, 3 MID, 4 FWD)
    team  = each player's club
    done  = clubs whose fixtures in this gameweek have all finished, so a
            player on 0 minutes there is definitely not playing
    """

    stats = get_live_stats(gameweek)

    pos = {}
    team = {}
    all_teams = set()

    try:
        boot = fpl_get("bootstrap-static/")

        for element in boot.get("elements", []):
            pos[element["id"]] = element.get("element_type", 3)
            team[element["id"]] = element.get("team")

        all_teams = {t["id"] for t in boot.get("teams", [])}

    except Exception:
        pass

    done = set()

    try:
        fixtures = fpl_get(f"fixtures/?event={gameweek}")

        pending = set()

        for f in fixtures:
            if not (f.get("finished") or f.get("finished_provisional")):
                pending.add(f.get("team_h"))
                pending.add(f.get("team_a"))

        # Clubs with no fixture this week count as done as well
        done = {t for t in all_teams if t not in pending}

    except Exception:
        done = set()

    return {"stats": stats, "pos": pos, "team": team, "done": done}


def score_live_team(picks_data, ctx):
    """A manager's gameweek points BEFORE any transfer hit, worked out the
    way the FPL website does it while games are being played:

    - Starting XI and bench order come from the manager's picks
    - Captain gets x2 (x3 on Triple Captain); the vice-captain takes over
      if the captain's games are finished and he didn't play
    - Automatic subs happen for starters on 0 minutes whose games are
      over (goalkeeper for goalkeeper, formation must stay legal)
    - Bench Boost counts all 15 players

    Returns {"gross", "captain", "captain_points"} or None if there are
    no picks.
    """

    picks = sorted(
        picks_data.get("picks", []),
        key=lambda p: p.get("position", 0)
    )

    if not picks:
        return None

    stats = ctx["stats"]
    pos = ctx["pos"]
    team = ctx["team"]
    done = ctx["done"]

    chip = picks_data.get("active_chip")

    def minutes(element):
        return stats.get(element, {}).get("minutes", 0)

    def points(element):
        return stats.get(element, {}).get("points", 0)

    def out_for_good(element):
        return minutes(element) == 0 and team.get(element) in done

    xi = [p["element"] for p in picks if p["position"] <= 11]
    bench = [p["element"] for p in picks if p["position"] > 11]

    def formation_ok(players):
        counts = {1: 0, 2: 0, 3: 0, 4: 0}

        for element in players:
            counts[pos.get(element, 3)] += 1

        return (
            counts[1] == 1 and counts[2] >= 3
            and counts[3] >= 2 and counts[4] >= 1
        )

    if chip == "bboost":

        xi = xi + bench

    else:

        used = set()

        for starter in list(xi):

            if not out_for_good(starter):
                continue

            for sub in bench:

                if sub in used or minutes(sub) == 0:
                    continue

                starter_gk = pos.get(starter, 3) == 1
                sub_gk = pos.get(sub, 3) == 1

                if starter_gk != sub_gk:
                    continue

                trial = [sub if x == starter else x for x in xi]

                if not starter_gk and not formation_ok(trial):
                    continue

                xi[xi.index(starter)] = sub
                used.add(sub)
                break

    captain = next(
        (p["element"] for p in picks if p.get("is_captain")), None
    )
    vice = next(
        (p["element"] for p in picks if p.get("is_vice_captain")), None
    )

    multiplier = 3 if chip == "3xc" else 2

    effective = captain

    if captain is not None and out_for_good(captain) and vice is not None:
        effective = vice

    total = sum(points(element) for element in xi)

    if effective is not None and effective in xi:
        total += points(effective) * (multiplier - 1)

    return {
        "gross": total,
        "captain": effective,
        "captain_points": (
            points(effective) * multiplier if effective is not None else 0
        )
    }


def get_previous_snapshot():
    """Get the most recent saved snapshot."""

    if not os.path.exists(HISTORY_FILE):
        return None

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as file:
            history = json.load(file)

        if not history:
            return None

        return history[-1]

    except Exception:
        return None


def load_history():
    """Load all historical snapshots."""

    if not os.path.exists(HISTORY_FILE):
        return []

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return []


def save_snapshot(snapshot):
    """Save a Gameweek snapshot."""

    history = load_history()

    # Don't save duplicate Gameweeks
    existing = [
        x for x in history
        if x.get("gameweek") == snapshot.get("gameweek")
    ]

    if existing:
        history = [
            x for x in history
            if x.get("gameweek") != snapshot.get("gameweek")
        ]

    history.append(snapshot)

    history.sort(
        key=lambda x: x.get("gameweek", 0)
    )

    with open(
        HISTORY_FILE,
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            history,
            file,
            indent=2
        )


# ============================================================
# LEAGUE MEMBERSHIP
# ============================================================

def load_league_state():
    """Load saved league membership and swap history."""

    if not os.path.exists(LEAGUES_FILE):
        return None

    try:
        with open(LEAGUES_FILE, "r", encoding="utf-8") as file:
            saved = json.load(file)
    except Exception:
        return None

    if not saved:
        return None

    # Upgrade the old format ({entry: league}) to the new one
    if "assignments" not in saved:
        saved = {
            "assignments": saved,
            "start_gw": None,
            "periods_done": 0,
            "swaps": []
        }

    return saved


def save_league_state(state):
    """Save league membership and swap history."""

    with open(LEAGUES_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)


def get_league_state(standings, gameweek):
    """Load the league state, or create it on first run."""

    state = load_league_state()

    if state is None:

        assignments = {}

        if MANUAL_LEAGUES["championship"] or MANUAL_LEAGUES["challenger"]:
            for entry in MANUAL_LEAGUES["championship"]:
                assignments[str(entry)] = "championship"
            for entry in MANUAL_LEAGUES["challenger"]:
                assignments[str(entry)] = "challenger"
        else:
            # Lock in the current top 5 / 6th-10th
            for index, row in enumerate(standings[:10]):
                assignments[str(row.get("entry"))] = (
                    "championship" if index < 5 else "challenger"
                )

        state = {
            "assignments": assignments,
            "start_gw": GAME_START_GW or (gameweek + 1),
            "periods_done": 0,
            "swaps": []
        }

        save_league_state(state)

    elif state.get("start_gw") is None:

        state["start_gw"] = GAME_START_GW or (gameweek + 1)
        save_league_state(state)

    return state


def cumulative_points(history, gameweek):
    """A manager's season points (after hits) up to and including a GW."""

    if gameweek < 1:
        return 0

    best_event = 0
    best_total = 0

    for gw in history.get("current", []):
        event = gw.get("event", 0)

        if best_event < event <= gameweek:
            best_event = event
            best_total = gw.get("total_points", 0)

    return best_total


def points_between(history, first_gw, last_gw):
    """Points scored from first_gw to last_gw inclusive."""

    return (
        cumulative_points(history, last_gw)
        - cumulative_points(history, first_gw - 1)
    )


def period_bounds(start_gw, period_index):
    """First and last Gameweek of a period (period_index starts at 0)."""

    first = start_gw + period_index * PERIOD_LENGTH

    return first, first + PERIOD_LENGTH - 1


def basis_start(period_first_gw):
    """First Gameweek counted when ranking inside a period."""

    return period_first_gw if RESET_POINTS_EACH_PERIOD else 1


def rank_members(state, league, histories, totals, first_gw, last_gw):
    """Entry IDs of one league, best first."""

    members = [
        int(entry)
        for entry, name in state["assignments"].items()
        if name == league and int(entry) in histories
    ]

    def sort_key(entry):
        points = points_between(
            histories[entry], first_gw, last_gw
        )
        return (-points, -totals.get(entry, 0))

    return sorted(members, key=sort_key)


def apply_pending_swaps(state, histories, totals, gameweek):
    """Promote/relegate for every period that has now finished."""

    changed = False

    while True:

        first, last = period_bounds(
            state["start_gw"],
            state["periods_done"]
        )

        # Period not finished yet
        if last > gameweek:
            break

        begin = basis_start(first)

        champ = rank_members(
            state, "championship", histories, totals, begin, last
        )
        chal = rank_members(
            state, "challenger", histories, totals, begin, last
        )

        # Period winners (1st in each league) win a curry
        winners = []

        if champ:
            winners.append(champ[0])
        if chal:
            winners.append(chal[0])

        relegated = []
        promoted = []

        if len(champ) >= 2 and len(chal) >= 2:

            relegated = champ[-2:]    # 4th & 5th
            promoted = chal[:2]       # 1st & 2nd

            for entry in relegated:
                state["assignments"][str(entry)] = "challenger"

            for entry in promoted:
                state["assignments"][str(entry)] = "championship"

        state["swaps"].append({
            "period": state["periods_done"] + 1,
            "gameweeks": [first, last],
            "relegated": relegated,
            "promoted": promoted,
            "winners": winners
        })

        state["periods_done"] += 1
        changed = True

    if changed:
        save_league_state(state)


def get_league_rows(standings, gameweek, display_gw=None):
    """Return (rows, state): league members ranked inside their league.

    gameweek   = last FINISHED gameweek (used for promotion/relegation)
    display_gw = gameweek being shown (live). None = skip live data.

    Also applies any promotion/relegation that is now due.
    """

    state = get_league_state(standings, gameweek)

    by_entry = {row["entry"]: row for row in standings}

    member_ids = [
        int(entry)
        for entry in state["assignments"]
        if int(entry) in by_entry
    ]

    histories = {
        entry: get_manager_history(entry)
        for entry in member_ids
    }

    totals = {
        entry: by_entry[entry].get("total", 0)
        for entry in member_ids
    }

    apply_pending_swaps(state, histories, totals, gameweek)

    # Current period (after any swaps)
    first, last = period_bounds(
        state["start_gw"],
        state["periods_done"]
    )

    begin = basis_start(first)

    live_gw = display_gw or gameweek
    end = max(min(last, gameweek), begin - 1)
    in_progress = bool(display_gw) and display_gw > gameweek

    live_points = get_live_points(live_gw) if display_gw else {}
    ctx = get_live_context(live_gw) if display_gw else None

    last_swap = state["swaps"][-1] if state["swaps"] else None

    rows = []

    for entry in member_ids:

        league = state["assignments"][str(entry)]

        # Curries won by this manager
        if CURRIES_STACK:
            curries = sum(
                1 for swap in state["swaps"]
                if entry in swap.get("winners", [])
            )
        elif last_swap and entry in last_swap.get("winners", []):
            curries = 1
        else:
            curries = 0

        last_move = None

        if last_swap:
            if entry in last_swap["promoted"]:
                last_move = "promoted"
            elif entry in last_swap["relegated"]:
                last_move = "relegated"

        history = histories[entry]

        # ---- Gameweek points (live if FPL hasn't finalised them) ----
        picks_data = {"picks": []}
        calc = None
        gw_points = 0
        gw_hit = 0
        extra = 0     # live points not yet in FPL's history

        if display_gw:

            picks_data = get_manager_picks(entry, live_gw)

            calc = score_live_team(picks_data, ctx) if ctx else None

            hist_row = next(
                (g for g in history.get("current", [])
                 if g.get("event") == live_gw),
                None
            )

            use_history = bool(hist_row) and not in_progress

            official = picks_data.get("entry_history") or {}

            if use_history:
                gross = hist_row.get("points", 0)
                gw_hit = hist_row.get("event_transfers_cost", 0)
            elif not in_progress and calc is None and "points" in official:
                # Gameweek has ended but isn't in the history yet:
                # use FPL's own figure instead of working it out here
                gross = official.get("points", 0)
                gw_hit = official.get("event_transfers_cost", 0)
            else:
                picks = picks_data.get("picks", [])

                # multiplier is 0 for benched players, 2 for captain, etc.
                gross = sum(
                    live_points.get(p["element"], 0) * p.get("multiplier", 1)
                    for p in picks
                )

                # Auto-subs, once FPL has applied them to the picks data
                multipliers = {
                    p["element"]: p.get("multiplier", 1) for p in picks
                }

                for sub in picks_data.get("automatic_subs", []):
                    gross += live_points.get(sub["element_in"], 0)
                    gross -= (
                        live_points.get(sub["element_out"], 0)
                        * multipliers.get(sub["element_out"], 1)
                    )

                gw_hit = picks_data.get("entry_history", {}).get(
                    "event_transfers_cost", 0
                )

                # Better live figure: includes auto-subs and the
                # vice-captain rule, like the FPL website
                if calc is not None:
                    gross = calc["gross"]

            # Green number = gameweek points AFTER the transfer hit
            gw_points = gross - gw_hit

            if not use_history and first <= live_gw <= last:
                extra = gw_points

        rows.append({
            **by_entry[entry],
            "league": league,
            "history": history,
            "overall_total": totals[entry],
            # period total = finished gameweeks + live gameweek
            "points": points_between(history, begin, end) + extra,
            "gw_points": gw_points,
            "gw_hit": gw_hit,
            "picks": picks_data,
            "live_calc": calc,
            "last_move": last_move,
            "curries": curries
        })

    for name in ("championship", "challenger"):

        group = sorted(
            [r for r in rows if r["league"] == name],
            key=lambda r: (-r["points"], -r["overall_total"])
        )

        for position, r in enumerate(group, start=1):
            r["league_rank"] = position

    rows.sort(
        key=lambda r: (
            0 if r["league"] == "championship" else 1,
            r["league_rank"]
        )
    )

    return rows, state


def load_hits():
    """Load the saved hits record."""

    if not os.path.exists(HITS_FILE):
        return {"updated": None, "managers": {}}

    try:
        with open(HITS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        data.setdefault("managers", {})
        return data

    except Exception:
        return {"updated": None, "managers": {}}


def record_hits(rows):
    """Save every transfer hit each manager has taken this season.

    A hit is any Gameweek where event_transfers_cost is above 0
    (-4 for one extra transfer, -8 for two, and so on).
    """

    data = load_hits()
    managers = data["managers"]

    for row in rows:

        current = row["history"].get("current", [])

        # If FPL didn't answer, keep what we already saved
        if not current:
            continue

        hits = []

        for gw in current:
            cost = gw.get("event_transfers_cost", 0)

            if cost > 0:
                hits.append({
                    "gw": gw.get("event"),
                    "points": cost
                })

        managers[str(row["entry"])] = {
            "manager": row.get("player_name", "Unknown Manager"),
            "team": row.get("entry_name", "Unknown Team"),
            "hits": hits,
            "total_points": sum(h["points"] for h in hits),
            "total_hits": sum(h["points"] // 4 for h in hits)
        }

    data["managers"] = managers
    data["updated"] = datetime.now().strftime("%d %b %Y %H:%M")

    with open(HITS_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)

    return data


def get_chip_status(history, chip_half):
    """Which chips a manager has used / still has in the current half.

    chip_half = 1 (GW1-19) or 2 (GW20-38).
    """

    if chip_half == 1:
        first_gw, last_gw = 1, CHIP_SET_SPLIT_GW
    else:
        first_gw, last_gw = CHIP_SET_SPLIT_GW + 1, 38

    used = {}

    for chip in history.get("chips", []):
        event = chip.get("event", 0)

        if first_gw <= event <= last_gw:
            used[chip.get("name")] = event

    return [
        {
            "key": key,
            "short": short,
            "name": name,
            "available": key not in used,
            "used_gw": used.get(key)
        }
        for key, short, name in CHIP_LIST
    ]


def load_bench_cache():
    """Load saved bench points ({entry: {gameweek: points}})."""

    if not os.path.exists(BENCH_FILE):
        return {"entries": {}}

    try:
        with open(BENCH_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        data.setdefault("entries", {})
        return data

    except Exception:
        return {"entries": {}}


def save_bench_cache(cache):
    """Save bench points so finished gameweeks are only fetched once."""

    with open(BENCH_FILE, "w", encoding="utf-8") as file:
        json.dump(cache, file, indent=2)


def compute_bench_points(entry_id, gameweek, live_points):
    """Points left on the bench in a finished gameweek.

    Bench players (positions 12-15) who were automatically subbed on
    are NOT counted, because their points went into the score.
    Returns None if FPL didn't answer (so it can be tried again).
    """

    picks_data = get_manager_picks(entry_id, gameweek)

    picks = picks_data.get("picks", [])

    if not picks:
        return None

    subbed_in = {
        sub.get("element_in")
        for sub in picks_data.get("automatic_subs", [])
    }

    return sum(
        live_points.get(p["element"], 0)
        for p in picks
        if p.get("position", 0) >= 12 and p["element"] not in subbed_in
    )


def bench_points_for(entry_id, gameweek, cache, live_cache):
    """Saved bench points for one manager + gameweek (fetched if new)."""

    entry_cache = cache.setdefault("entries", {}).setdefault(
        str(entry_id), {}
    )

    if str(gameweek) in entry_cache:
        return entry_cache[str(gameweek)]

    if gameweek not in live_cache:
        live_cache[gameweek] = get_live_points(gameweek)

    if not live_cache[gameweek]:
        return None

    points = compute_bench_points(
        entry_id, gameweek, live_cache[gameweek]
    )

    if points is not None:
        entry_cache[str(gameweek)] = points

    return points


def load_chip_cache():
    """Load saved Triple Captain results."""

    if not os.path.exists(CHIPS_FILE):
        return {"tc": {}}

    try:
        with open(CHIPS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        data.setdefault("tc", {})
        return data

    except Exception:
        return {"tc": {}}


def save_chip_cache(cache):
    """Save Triple Captain results."""

    with open(CHIPS_FILE, "w", encoding="utf-8") as file:
        json.dump(cache, file, indent=2)


def find_tc_pick(picks):
    """The pick that was triple captained (multiplier 3)."""

    for pick in picks:
        if pick.get("multiplier") == 3:
            return pick

    for pick in picks:
        if pick.get("is_captain"):
            return pick

    return None


def tc_result_for(entry_id, gameweek, cache, live_cache, player_names):
    """Who was triple captained in a finished gameweek and their points.

    The points include the x3 multiplier. Returns None if FPL didn't
    answer (so it is tried again next time).
    """

    store = cache.setdefault("tc", {}).setdefault(str(entry_id), {})

    if str(gameweek) in store:
        return store[str(gameweek)]

    if gameweek not in live_cache:
        live_cache[gameweek] = get_live_points(gameweek)

    live = live_cache[gameweek]

    if not live:
        return None

    picks = get_manager_picks(entry_id, gameweek).get("picks", [])

    pick = find_tc_pick(picks)

    if not pick:
        return None

    result = {
        "player": player_names.get(pick["element"], "Unknown"),
        "points": live.get(pick["element"], 0) * pick.get("multiplier", 3)
    }

    store[str(gameweek)] = result

    return result


def build_snapshot():
    """Build a complete current league snapshot."""

    league_name, standings = get_league()

    finished_gw = get_current_gameweek()     # for promotion/relegation
    gameweek = get_live_gameweek()           # everything displayed

    league_rows, state = get_league_rows(standings, finished_gw, gameweek)

    try:
        record_hits(league_rows)
    except Exception:
        pass

    period_first, period_last = period_bounds(
        state["start_gw"],
        state["periods_done"]
    )

    period_number = state["periods_done"] + 1

    # Chips: which set of chips is currently in play
    chip_half = 1 if gameweek < CHIP_SET_SPLIT_GW else 2

    # Fetched once per update, shared by every manager
    player_names = get_player_names()
    live_points = get_live_points(gameweek)

    previous = get_previous_snapshot()

    previous_period = (previous or {}).get("period", {}).get("number")

    previous_players = {}

    if previous:
        for player in previous.get("players", []):
            previous_players[player["entry"]] = player

    players = []

    bench_cache = load_bench_cache()
    chip_cache = load_chip_cache()
    bench_live_cache = {}

    for row in league_rows:

        entry_id = row.get("entry")

        manager_name = row.get(
            "player_name",
            "Unknown Manager"
        )

        team_name = row.get(
            "entry_name",
            "Unknown Team"
        )

        rank = row["league_rank"]      # position WITHIN their league
        league = row["league"]

        total = row["points"]    # period total (includes live gameweek)

        # ----------------------------------------------------
        # Gameweek points (live) - worked out in get_league_rows
        # ----------------------------------------------------

        gw_points = row["gw_points"]
        hit_cost = row["gw_hit"]

        # ----------------------------------------------------
        # Captain for this Gameweek
        # ----------------------------------------------------

        captain_name = "—"
        captain_points = 0

        picks_data = row["picks"]

        for pick in picks_data.get("picks", []):
            if pick.get("is_captain"):
                element_id = pick["element"]
                captain_name = player_names.get(element_id, "Unknown")
                captain_points = (
                    live_points.get(element_id, 0)
                    * pick.get("multiplier", 2)
                )
                break

        # The vice-captain takes over if the captain didn't play
        live_calc = row.get("live_calc")

        if live_calc and live_calc.get("captain") is not None:
            captain_name = player_names.get(
                live_calc["captain"], "Unknown"
            )
            captain_points = live_calc["captain_points"]

        # ----------------------------------------------------
        # Previous position (within the same league)
        # ----------------------------------------------------

        previous_rank = None

        prev = previous_players.get(entry_id)

        # Only compare within the same league AND the same period
        if (
            prev
            and prev.get("league") == league
            and previous_period == period_number
        ):
            previous_rank = prev.get("rank")

        movement = 0

        if previous_rank is not None:
            movement = previous_rank - rank

        # ----------------------------------------------------
        # Points left on the bench (finished gameweeks only)
        # ----------------------------------------------------

        history_rows = row["history"].get("current", [])

        # Bench Boost weeks: bench points counted, so nothing was wasted
        bboost_weeks = {
            c.get("event")
            for c in row["history"].get("chips", [])
            if c.get("name") == "bboost"
        }

        bench_by_gw = []

        for gw_row in history_rows:

            event = gw_row.get("event", 0)

            if event <= finished_gw and event not in bboost_weeks:
                computed = bench_points_for(
                    entry_id, event, bench_cache, bench_live_cache
                )

                bench_by_gw.append({
                    "gw": event,
                    "points": (
                        computed if computed is not None
                        else (gw_row.get("points_on_bench", 0) or 0)
                    )
                })

        bench_total = sum(b["points"] for b in bench_by_gw)

        bench_last = next(
            (b["points"] for b in bench_by_gw if b["gw"] == finished_gw),
            0
        )

        # ----------------------------------------------------
        # Chip scores
        #   Wildcard / Free Hit / Bench Boost = whole team's points
        #   Triple Captain = only the captained player's points
        # ----------------------------------------------------

        chip_results = {
            "wildcard": [],
            "freehit": [],
            "bboost": [],
            "3xc": []
        }

        for chip in row["history"].get("chips", []):

            chip_name = chip.get("name")
            chip_event = chip.get("event", 0)

            if chip_name not in chip_results or chip_event > gameweek:
                continue

            live_week = chip_event > finished_gw

            if chip_name == "3xc":

                result = None

                if live_week:
                    pick = find_tc_pick(row["picks"].get("picks", []))

                    if pick:
                        result = {
                            "player": player_names.get(
                                pick["element"], "Unknown"
                            ),
                            "points": (
                                live_points.get(pick["element"], 0)
                                * pick.get("multiplier", 3)
                            )
                        }
                else:
                    result = tc_result_for(
                        entry_id, chip_event, chip_cache,
                        bench_live_cache, player_names
                    )

                if result:
                    chip_results[chip_name].append(
                        {"gw": chip_event, "live": live_week, **result}
                    )

            else:

                if live_week:
                    chip_points = row["gw_points"] + row["gw_hit"]
                else:
                    chip_hist = next(
                        (g for g in row["history"].get("current", [])
                         if g.get("event") == chip_event),
                        None
                    )
                    chip_points = (
                        chip_hist.get("points", 0) if chip_hist else None
                    )

                if chip_points is not None:
                    chip_results[chip_name].append({
                        "gw": chip_event,
                        "live": live_week,
                        "points": chip_points
                    })

        for results in chip_results.values():
            results.sort(key=lambda r: r["gw"])

        players.append({
            "entry": entry_id,
            "rank": rank,
            "league": league,
            "manager": manager_name,
            "team": team_name,
            "total": total,
            "overall_total": row["overall_total"],
            "last_move": row["last_move"],
            "curries": row["curries"],
            "chips": get_chip_status(row["history"], chip_half),
            "hit_cost": hit_cost,
            "gw_points": gw_points,
            "captain": captain_name,
            "captain_points": captain_points,
            "previous_rank": previous_rank,
            "movement": movement,
            "bench_total": bench_total,
            "bench_last": bench_last,
            "bench_by_gw": bench_by_gw,
            "chip_results": chip_results
        })

    try:
        save_bench_cache(bench_cache)
        save_chip_cache(chip_cache)
    except Exception:
        pass

    players.sort(
        key=lambda x: (
            0 if x["league"] == "championship" else 1,
            x["rank"]
        )
    )

    snapshot = {
        "gameweek": gameweek,
        "live": gameweek > finished_gw,
        "bench_gw": finished_gw,
        "league_name": league_name,
        "updated": datetime.now().strftime(
            "%d %b %Y %H:%M"
        ),
        "chip_half": chip_half,
        "chip_split_gw": CHIP_SET_SPLIT_GW,
        "period": {
            "number": period_number,
            "first_gw": period_first,
            "last_gw": period_last,
            "reset": RESET_POINTS_EACH_PERIOD
        },
        "players": players
    }

    return snapshot

# ============================================================
# WHATSAPP GRAPHIC
# ============================================================

def render_emoji_icon(char, size):
    """Render an emoji as a small RGBA image using a colour-emoji font.

    Returns None if no suitable font can be found on this computer.
    """

    candidates = [
        ("seguiemj.ttf", 109),                                   # Windows
        ("/System/Library/Fonts/Apple Color Emoji.ttc", 160),    # Mac
        ("NotoColorEmoji.ttf", 109),                             # Linux
        ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", 109)
    ]

    for name, font_size in candidates:
        try:
            font = ImageFont.truetype(name, font_size)

            canvas = Image.new("RGBA", (260, 260), (0, 0, 0, 0))

            ImageDraw.Draw(canvas).text(
                (10, 10),
                char,
                font=font,
                embedded_color=True
            )

            box = canvas.getbbox()

            if box:
                glyph = canvas.crop(box)
                glyph.thumbnail((size, size), Image.LANCZOS)

                icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                icon.paste(
                    glyph,
                    ((size - glyph.width) // 2, (size - glyph.height) // 2)
                )

                return icon

        except Exception:
            continue

    return None


_CURRY_ICONS = {}
_CLOWN_ICONS = {}


def get_curry_icon(size):
    """A small curry picture for the PNGs.

    Uses the system colour-emoji font if one can be found,
    otherwise draws a simple curry bowl.
    """

    if size in _CURRY_ICONS:
        return _CURRY_ICONS[size]

    icon = render_emoji_icon("🍛", size)

    if icon is None:

        # Fallback: draw a little bowl of curry
        s = size * 4

        canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(canvas)

        # Bowl
        d.pieslice(
            (s * 0.05, s * 0.12, s * 0.95, s * 1.0),
            0, 180,
            fill="#F5F5F5",
            outline="#B0B0B0",
            width=max(2, s // 40)
        )

        # Curry
        d.ellipse(
            (s * 0.12, s * 0.40, s * 0.88, s * 0.70),
            fill="#E8892B",
            outline="#B8641A",
            width=max(2, s // 40)
        )

        # Rice / highlight
        d.ellipse(
            (s * 0.30, s * 0.46, s * 0.55, s * 0.58),
            fill="#F4B25F"
        )

        icon = canvas.resize((size, size), Image.LANCZOS)

    _CURRY_ICONS[size] = icon

    return icon


def get_clown_icon(size):
    """A small clown picture for the PNGs."""

    if size in _CLOWN_ICONS:
        return _CLOWN_ICONS[size]

    icon = render_emoji_icon("🤡", size)

    if icon is None:

        # Fallback: draw a simple clown face
        s = size * 4

        canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(canvas)

        line = max(2, s // 40)

        # Hair
        d.ellipse((s * 0.00, s * 0.20, s * 0.34, s * 0.54), fill="#E63946")
        d.ellipse((s * 0.66, s * 0.20, s * 1.00, s * 0.54), fill="#E63946")

        # Face
        d.ellipse(
            (s * 0.14, s * 0.08, s * 0.86, s * 0.94),
            fill="#FFF5E6",
            outline="#B0A090",
            width=line
        )

        # Eyes
        d.ellipse((s * 0.32, s * 0.30, s * 0.42, s * 0.42), fill="#222222")
        d.ellipse((s * 0.58, s * 0.30, s * 0.68, s * 0.42), fill="#222222")

        # Nose
        d.ellipse((s * 0.42, s * 0.44, s * 0.58, s * 0.60), fill="#E63946")

        # Smile
        d.arc(
            (s * 0.30, s * 0.50, s * 0.70, s * 0.82),
            20, 160,
            fill="#E63946",
            width=line * 2
        )

        icon = canvas.resize((size, size), Image.LANCZOS)

    _CLOWN_ICONS[size] = icon

    return icon


_STAR_ICONS = {}


def get_star_icon(size):
    """A small gold star picture for the PNGs."""

    if size in _STAR_ICONS:
        return _STAR_ICONS[size]

    icon = render_emoji_icon("⭐", size)

    if icon is None:

        # Fallback: draw a simple gold star
        s = size * 4

        canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(canvas)

        centre = s / 2
        points = []

        for i in range(10):
            radius = s * 0.48 if i % 2 == 0 else s * 0.20
            angle = -math.pi / 2 + i * math.pi / 5

            points.append((
                centre + radius * math.cos(angle),
                centre + radius * math.sin(angle)
            ))

        d.polygon(points, fill="#FFD700", outline="#B8860B")

        icon = canvas.resize((size, size), Image.LANCZOS)

    _STAR_ICONS[size] = icon

    return icon


def create_whatsapp_graphic(snapshot):
    """Create a WhatsApp-friendly PNG of the league tables."""

    width = 1400
    height = 1050

    # FPL-style colours
    green = "#00FF87"
    dark = "#061A14"
    card = "#0A241B"
    white = "#FFFFFF"
    grey = "#9AB3A8"
    red = "#FF4D6D"
    gold = "#FFD700"

    image = Image.new(
        "RGB",
        (width, height),
        dark
    )

    draw = ImageDraw.Draw(image)

    # Fonts
    try:
        title_font = ImageFont.truetype(
            "DejaVuSans-Bold.ttf", 52
        )
        subtitle_font = ImageFont.truetype(
            "DejaVuSans-Bold.ttf", 28
        )
        name_font = ImageFont.truetype(
            "DejaVuSans-Bold.ttf", 24
        )
        small_font = ImageFont.truetype(
            "DejaVuSans.ttf", 18
        )
        points_font = ImageFont.truetype(
            "DejaVuSans-Bold.ttf", 26
        )
    except Exception:
        title_font = ImageFont.load_default()
        subtitle_font = ImageFont.load_default()
        name_font = ImageFont.load_default()
        small_font = ImageFont.load_default()
        points_font = ImageFont.load_default()

    # Header
    draw.rectangle(
        (0, 0, width, 145),
        fill=green
    )

    draw.text(
        (50, 25),
        "⚽ FPL LEAGUE",
        fill=dark,
        font=title_font
    )

    draw.text(
        (52, 88),
        (
            f"{snapshot['league_name']}  •  GAMEWEEK {snapshot['gameweek']}"
            f"  •  PERIOD {snapshot['period']['number']}"
            f" (GW {snapshot['period']['first_gw']}"
            f"-{snapshot['period']['last_gw']})"
        ),
        fill=dark,
        font=small_font
    )

    # Split by fixed league membership
    top5 = [
        p for p in snapshot["players"]
        if p["league"] == "championship"
    ]
    bottom5 = [
        p for p in snapshot["players"]
        if p["league"] == "challenger"
    ]

    points_label = (
        "PERIOD" if snapshot["period"]["reset"] else "TOTAL"
    )

    tag_font = load_font(True, 15)
    chip_font = load_font(True, 13)

    gw_label = f"GW{snapshot['gameweek']}" + (
        " LIVE" if snapshot.get("live") else ""
    )

    def draw_league_card(
        x,
        y,
        w,
        h,
        title,
        subtitle,
        players
    ):

        # Card
        draw.rounded_rectangle(
            (x, y, x + w, y + h),
            radius=25,
            fill=card,
            outline="#174D3A",
            width=3
        )

        # Heading
        draw.text(
            (x + 25, y + 20),
            title,
            fill=white,
            font=subtitle_font
        )

        draw.text(
            (x + 25, y + 58),
            subtitle,
            fill=grey,
            font=small_font
        )

        row_y = y + 105

        for player in players:

            # Row background
            draw.rounded_rectangle(
                (
                    x + 15,
                    row_y,
                    x + w - 15,
                    row_y + 90
                ),
                radius=12,
                fill="#0D3024"
            )

            rank = player["rank"]

            if rank == 1:
                medal = "🥇"
            elif rank == 2:
                medal = "🥈"
            elif rank == 3:
                medal = "🥉"
            else:
                medal = str(rank)

            # Rank
            draw.text(
                (x + 30, row_y + 28),
                medal,
                fill=gold if rank == 1 else white,
                font=name_font
            )

            # Manager (+ curry for period winners, + clown for hits)
            manager = player["manager"]

            curries = player.get("curries", 0)
            hit_cost = player.get("hit_cost", 0)

            hit_text = f"-{hit_cost}"

            # Shorten the name if the extras need the room
            extras_w = 0

            if curries:
                extras_w += 36
                if curries > 1:
                    extras_w += 32

            if hit_cost:
                extras_w += (
                    32
                    + draw.textlength(hit_text, font=small_font)
                    + 6
                )

            max_name_w = (w - 205) - 95 - 12 - extras_w

            manager_text = manager[:24]

            while (
                manager_text
                and draw.textlength(manager_text, font=name_font) > max_name_w
            ):
                manager_text = manager_text[:-1]

            draw.text(
                (x + 95, row_y + 18),
                manager_text,
                fill=white,
                font=name_font
            )

            cursor_x = int(
                x + 95
                + draw.textlength(manager_text, font=name_font)
                + 8
            )

            if curries:
                icon = get_curry_icon(28)

                image.paste(icon, (cursor_x, row_y + 17), icon)

                cursor_x += 34

                if curries > 1:
                    draw.text(
                        (cursor_x, row_y + 23),
                        f"x{curries}",
                        fill=gold,
                        font=small_font
                    )

                    cursor_x += 32

            if hit_cost:
                icon = get_clown_icon(28)

                image.paste(icon, (cursor_x, row_y + 17), icon)

                cursor_x += 32

                draw.text(
                    (cursor_x, row_y + 23),
                    hit_text,
                    fill=red,
                    font=small_font
                )

            # Team (+ promoted / relegated tag)
            last_move = player.get("last_move")

            if last_move:
                team_text = player["team"][:20]
            else:
                team_text = player["team"][:28]

            draw.text(
                (x + 95, row_y + 50),
                team_text,
                fill=grey,
                font=small_font
            )

            if last_move:
                tag_x = (
                    x + 95
                    + draw.textlength(team_text, font=small_font)
                    + 10
                )

                if last_move == "promoted":
                    tag_text = "▲ PROMOTED"
                    tag_colour = green
                else:
                    tag_text = "▼ RELEGATED"
                    tag_colour = red

                draw.text(
                    (tag_x, row_y + 54),
                    tag_text,
                    fill=tag_colour,
                    font=tag_font
                )

            # Chips (green = available, struck through = used)
            chip_x = x + 95

            for chip in player.get("chips", []):

                chip_colour = green if chip["available"] else "#56705F"

                draw.text(
                    (chip_x, row_y + 71),
                    chip["short"],
                    fill=chip_colour,
                    font=chip_font
                )

                chip_w = draw.textlength(chip["short"], font=chip_font)

                if not chip["available"]:
                    draw.line(
                        (
                            chip_x - 2, row_y + 79,
                            chip_x + chip_w + 2, row_y + 79
                        ),
                        fill=chip_colour,
                        width=2
                    )

                chip_x += chip_w + 14

            # Period total (white)
            draw.text(
                (x + w - 205, row_y + 18),
                str(player["total"]),
                fill=white,
                font=points_font
            )

            draw.text(
                (x + w - 205, row_y + 52),
                points_label,
                fill=grey,
                font=small_font
            )

            # Live gameweek points (green)
            gw_value = player["gw_points"]

            gw_text = f"+{gw_value}" if gw_value >= 0 else str(gw_value)

            gw_colour = green if gw_value >= 0 else red

            draw.text(
                (x + w - 115, row_y + 18),
                gw_text,
                fill=gw_colour,
                font=points_font
            )

            draw.text(
                (x + w - 115, row_y + 52),
                gw_label,
                fill=gw_colour,
                font=small_font
            )

            # Movement
            movement = player.get("movement", 0)

            if movement > 0:
                movement_text = f"↑ {movement}"
                movement_colour = green
            elif movement < 0:
                movement_text = f"↓ {abs(movement)}"
                movement_colour = red
            else:
                movement_text = "—"
                movement_colour = grey

            draw.text(
                (x + 30, row_y + 65),
                movement_text,
                fill=movement_colour,
                font=small_font
            )

            row_y += 105

    # Two league cards
    draw_league_card(
        40,
        175,
        640,
        680,
        "🏆 TOP 5",
        "CHAMPIONSHIP LEAGUE",
        top5
    )

    draw_league_card(
        720,
        175,
        640,
        680,
        "⚔️ 6TH — 10TH",
        "CHALLENGER LEAGUE",
        bottom5
    )

    # Footer
    draw.text(
        (50, 900),
        f"Updated: {snapshot['updated']}",
        fill=grey,
        font=small_font
    )

    draw.text(
        (50, 940),
        "Fantasy Premier League Mini League",
        fill=green,
        font=small_font
    )

    if snapshot.get("chip_half", 1) == 1:
        chip_note = (
            "Chips: green = available, struck through = used  •  "
            f"first-set chips expire after the GW{snapshot['chip_split_gw']} "
            "deadline"
        )
    else:
        chip_note = (
            "Chips: green = available, struck through = used  •  "
            "second-set chips (GW"
            f"{snapshot['chip_split_gw'] + 1}-38)"
        )

    draw.text(
        (50, 980),
        chip_note,
        fill=grey,
        font=small_font
    )

    draw.text(
        (50, 1010),
        "Clown = transfer points hit taken this Gameweek",
        fill=grey,
        font=small_font
    )

    # Return image in memory
    output = BytesIO()

    image.save(
        output,
        format="PNG"
    )

    output.seek(0)

    return output


# ============================================================
# TRANSFERS GRAPHIC
# ============================================================

def get_manager_transfers(entry_id):
    """Get every transfer a manager has made this season."""
    try:
        return fpl_get(f"entry/{entry_id}/transfers/")
    except Exception:
        return []


def load_parade_cache():
    """Saved hits parade results ({gameweek: {entry: {stars, clowns}}})."""

    rules = f"hits_only={PARADE_HITS_ONLY},deduct_hit={PARADE_DEDUCT_HIT}"

    if not os.path.exists(PARADE_FILE):
        return {"rules": rules, "gameweeks": {}}

    try:
        with open(PARADE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        # Saved with different rules (e.g. hits only)? Start again
        if data.get("rules") != rules:
            return {"rules": rules, "gameweeks": {}}

        data.setdefault("gameweeks", {})
        return data

    except Exception:
        return {"rules": rules, "gameweeks": {}}


def save_parade_cache(cache):
    """Save the hits parade so finished gameweeks are only fetched once."""

    with open(PARADE_FILE, "w", encoding="utf-8") as file:
        json.dump(cache, file, indent=2)


def judge_transfers(transfers, hit_cost, live_points):
    """Work out a star / clown for every transfer in one Gameweek.

    Free transfers are used first, so the LAST transfers made in the
    Gameweek are the ones that cost the -4 hits (those get the -4
    taken off before judging).

    Star  = player in outscored player out (after the -4 if it was a hit)
    Clown = player out outscored player in
    Level = no icon
    Returns a list (oldest transfer first).
    """

    transfers = sorted(transfers, key=lambda t: t.get("time", ""))

    hits_taken = max(0, hit_cost // 4)
    first_hit = max(0, len(transfers) - hits_taken)

    rows = []

    for index, t in enumerate(transfers):
        out_id = t["element_out"]
        in_id = t["element_in"]

        out_points = live_points.get(out_id, 0)
        in_points = live_points.get(in_id, 0)

        is_hit = index >= first_hit and hits_taken > 0

        net = in_points - out_points

        if is_hit and PARADE_DEDUCT_HIT:
            net -= 4

        if PARADE_HITS_ONLY and not is_hit:
            verdict = None
        elif net > 0:
            verdict = "star"
        elif net < 0:
            verdict = "clown"
        else:
            verdict = None

        rows.append({
            "out_id": out_id,
            "in_id": in_id,
            "out_points": out_points,
            "in_points": in_points,
            "is_hit": is_hit,
            "net": net,
            "verdict": verdict
        })

    return rows


def count_verdicts(rows):
    """(stars, clowns) in a list of judged transfers."""

    stars = sum(1 for r in rows if r["verdict"] == "star")
    clowns = sum(1 for r in rows if r["verdict"] == "clown")

    return stars, clowns


def get_parade_tally(league_rows, up_to_gw, live_gw, all_transfers):
    """Season-long stars and clowns per manager, up to a Gameweek.

    Finished gameweeks are saved and only fetched once.  The Gameweek
    still being played is worked out fresh every time (never saved).
    Returns {entry_id: {"stars": n, "clowns": n}}.
    """

    cache = load_parade_cache()
    gameweeks = cache["gameweeks"]

    live_cache = {}
    changed = False

    tally = {}

    for row in league_rows:

        entry_id = row.get("entry")

        history = row.get("history") or {}
        current = history.get("current", [])

        # If FPL didn't answer, don't guess (and don't save zeros)
        if not current:
            tally[entry_id] = {"stars": 0, "clowns": 0}
            continue

        cost_by_gw = {
            gw.get("event"): gw.get("event_transfers_cost", 0)
            for gw in current
        }

        if live_gw and live_gw not in cost_by_gw:
            cost_by_gw[live_gw] = row.get("gw_hit", 0)

        transfers_by_gw = {}

        for t in all_transfers.get(entry_id, []):
            transfers_by_gw.setdefault(t.get("event"), []).append(t)

        stars = 0
        clowns = 0

        for gw in range(1, up_to_gw + 1):

            hit_cost = cost_by_gw.get(gw, 0)
            gw_transfers = transfers_by_gw.get(gw, [])

            # No hit and hits-only mode = nothing to judge
            if PARADE_HITS_ONLY and hit_cost <= 0:
                continue

            if not gw_transfers:
                continue

            # The Gameweek being played is never saved
            finished = gw < live_gw

            saved = gameweeks.get(str(gw), {}).get(str(entry_id))

            if finished and saved is not None:
                stars += saved["stars"]
                clowns += saved["clowns"]
                continue

            if gw not in live_cache:
                live_cache[gw] = get_live_points(gw)

            if not live_cache[gw]:
                continue

            judged = judge_transfers(gw_transfers, hit_cost, live_cache[gw])
            gw_stars, gw_clowns = count_verdicts(judged)

            stars += gw_stars
            clowns += gw_clowns

            if finished:
                gameweeks.setdefault(str(gw), {})[str(entry_id)] = {
                    "stars": gw_stars,
                    "clowns": gw_clowns
                }
                changed = True

        tally[entry_id] = {"stars": stars, "clowns": clowns}

    if changed:
        try:
            save_parade_cache(cache)
        except Exception:
            pass

    return tally


def build_transfers_data(gameweek):
    """Collect each manager's transfers for one Gameweek."""

    league_name, standings = get_league()
    player_names = get_player_names()
    live_points = get_live_points(gameweek)

    # Same league members as the dashboard (finished gameweek keeps
    # promotion/relegation in sync, live gameweek keeps ranks in sync)
    finished_gw = get_current_gameweek()
    live_gw = get_live_gameweek()

    league_rows, _ = get_league_rows(standings, finished_gw, live_gw)

    chip_labels = {
        "wildcard": "WILDCARD",
        "freehit": "FREE HIT",
        "bboost": "BENCH BOOST",
        "3xc": "TRIPLE CAPTAIN",
        "manager": "ASSISTANT MANAGER"
    }

    # Every transfer each manager has made all season (fetched once)
    all_transfers = {
        row.get("entry"): get_manager_transfers(row.get("entry"))
        for row in league_rows
    }

    tally = get_parade_tally(league_rows, gameweek, live_gw, all_transfers)

    managers = []

    for row in league_rows:

        entry_id = row.get("entry")

        history = row["history"]

        hit_cost = 0

        if gameweek == live_gw:
            hit_cost = row["gw_hit"]
        else:
            for gw in history.get("current", []):
                if gw.get("event") == gameweek:
                    hit_cost = gw.get("event_transfers_cost", 0)
                    break

        chips = [
            chip_labels.get(c["name"], c["name"].upper())
            for c in history.get("chips", [])
            if c.get("event") == gameweek
        ]

        transfers = [
            t for t in all_transfers.get(entry_id, [])
            if t.get("event") == gameweek
        ]

        judged = judge_transfers(transfers, hit_cost, live_points)

        transfer_rows = []

        for j in judged:
            transfer_rows.append({
                "out": player_names.get(j["out_id"], "Unknown"),
                "out_points": j["out_points"],
                "in": player_names.get(j["in_id"], "Unknown"),
                "in_points": j["in_points"],
                "is_hit": j["is_hit"],
                "net": j["net"],
                "verdict": j["verdict"]
            })

        # Totals for the summary line (points hit paid once, as before)
        in_total = sum(t["in_points"] for t in transfer_rows)
        out_total = sum(t["out_points"] for t in transfer_rows)

        net = in_total - out_total - hit_cost

        season = tally.get(entry_id, {"stars": 0, "clowns": 0})

        managers.append({
            "rank": row["league_rank"],
            "league": row["league"],
            "curries": row["curries"],
            "manager": row.get("player_name", "Unknown Manager"),
            "team": row.get("entry_name", "Unknown Team"),
            "hit_cost": hit_cost,
            "chips": chips,
            "transfers": transfer_rows,
            "in_total": in_total,
            "out_total": out_total,
            "net": net,
            "stars": season["stars"],
            "clowns": season["clowns"]
        })

    managers.sort(
        key=lambda m: (
            0 if m["league"] == "championship" else 1,
            m["rank"]
        )
    )

    return {
        "league_name": league_name,
        "gameweek": gameweek,
        "updated": datetime.now().strftime("%d %b %Y %H:%M"),
        "managers": managers
    }


def load_font(bold, size):
    """Try several common font files so it works on Windows, Mac and Linux."""

    if bold:
        candidates = [
            "DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        ]
    else:
        candidates = [
            "DejaVuSans.ttf", "arial.ttf", "Arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ]

    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue

    return ImageFont.load_default()


def create_transfers_graphic(data):
    """Create a PNG showing every manager's transfers."""

    green = "#00FF87"
    dark = "#061A14"
    card = "#0A241B"
    row_bg = "#0D3024"
    white = "#FFFFFF"
    grey = "#9AB3A8"
    red = "#FF4D6D"
    gold = "#FFD700"

    title_font = load_font(True, 52)
    name_font = load_font(True, 24)
    player_font = load_font(True, 22)
    small_font = load_font(False, 18)

    width = 1400
    col_w = 640
    transfer_h = 38
    block_gap = 15

    def block_height(manager):
        lines = max(1, len(manager["transfers"]))
        return 85 + lines * transfer_h + 15

    managers = data["managers"]
    left = [m for m in managers if m["league"] == "championship"]
    right = [m for m in managers if m["league"] == "challenger"]

    def column_height(column):
        if not column:
            return 0
        return sum(block_height(m) for m in column) + block_gap * (len(column) - 1)

    content_h = max(column_height(left), column_height(right))
    height = 145 + 30 + content_h + 140

    image = Image.new("RGB", (width, height), dark)
    draw = ImageDraw.Draw(image)

    def right_text(x_right, y, text, fill, font):
        w = draw.textlength(text, font=font)
        draw.text((x_right - w, y), text, fill=fill, font=font)

    # Header
    draw.rectangle((0, 0, width, 145), fill=green)
    draw.text((50, 25), "FPL TRANSFERS", fill=dark, font=title_font)
    draw.text(
        (52, 95),
        f"{data['league_name']}  •  GAMEWEEK {data['gameweek']}",
        fill=dark,
        font=small_font
    )

    def draw_column(x, column):

        y = 175

        for m in column:

            h = block_height(m)

            draw.rounded_rectangle(
                (x, y, x + col_w, y + h),
                radius=20,
                fill=card,
                outline="#174D3A",
                width=3
            )

            # Manager + team
            name_text = f"{m['rank']}. {m['manager'][:26]}"

            draw.text(
                (x + 25, y + 15),
                name_text,
                fill=white,
                font=name_font
            )

            curries = m.get("curries", 0)

            if curries:
                icon_x = int(
                    x + 25
                    + draw.textlength(name_text, font=name_font)
                    + 8
                )

                icon = get_curry_icon(28)

                image.paste(icon, (icon_x, y + 14), icon)

                if curries > 1:
                    draw.text(
                        (icon_x + 32, y + 20),
                        f"x{curries}",
                        fill=green,
                        font=small_font
                    )

            # Summary shown top right (also used to check for overlap)
            summary = None

            if m["transfers"]:
                if m["hit_cost"]:
                    summary = (
                        f"IN {m['in_total']} • OUT {m['out_total']} "
                        f"• HIT -{m['hit_cost']} ({m['net']:+d})"
                    )
                else:
                    summary = (
                        f"IN {m['in_total']} • OUT {m['out_total']} "
                        f"({m['net']:+d})"
                    )

            # Hits parade score: stars v clowns for the season so far.
            # Sits next to the name; drops to the team line if the
            # name is too long to leave room for it.
            stars = m.get("stars", 0)
            clowns = m.get("clowns", 0)

            star_text = str(stars)
            clown_text = str(clowns)

            tally_w = (
                30 + int(draw.textlength(star_text, font=player_font)) + 12
                + 30 + int(draw.textlength(clown_text, font=player_font))
            )

            icon_x = int(
                x + 25
                + draw.textlength(name_text, font=name_font)
                + 12
            )

            if curries:
                icon_x += 34

                if curries > 1:
                    icon_x += 32

            summary_left = x + col_w - 25

            if summary:
                summary_left -= int(draw.textlength(summary, font=small_font))

            tally_y = y + 14

            if icon_x + tally_w > summary_left - 12:
                icon_x = int(
                    x + 25
                    + draw.textlength(m["team"][:34], font=small_font)
                    + 14
                )
                tally_y = y + 46

            star_icon = get_star_icon(26)
            image.paste(star_icon, (icon_x, tally_y), star_icon)
            icon_x += 30

            draw.text(
                (icon_x, tally_y + 3),
                star_text,
                fill=gold,
                font=player_font
            )
            icon_x += int(draw.textlength(star_text, font=player_font)) + 12

            clown_icon = get_clown_icon(26)
            image.paste(clown_icon, (icon_x, tally_y), clown_icon)
            icon_x += 30

            draw.text(
                (icon_x, tally_y + 3),
                clown_text,
                fill=red,
                font=player_font
            )

            draw.text(
                (x + 25, y + 48),
                m["team"][:34],
                fill=grey,
                font=small_font
            )

            # In vs out points / chip / hit cost (top right)
            tag_y = y + 18

            if m["transfers"]:
                diff = m["net"]

                if diff > 0:
                    verdict_colour = gold
                elif diff < 0:
                    verdict_colour = red
                else:
                    verdict_colour = grey

                right_text(
                    x + col_w - 25, tag_y,
                    summary,
                    verdict_colour, small_font
                )
                tag_y += 22

            if m["chips"]:
                right_text(
                    x + col_w - 25, tag_y,
                    " + ".join(m["chips"]),
                    green, small_font
                )
                tag_y += 22

            if m["hit_cost"]:
                right_text(
                    x + col_w - 25, tag_y,
                    f"-{m['hit_cost']} hit",
                    red, small_font
                )

            # Transfers
            row_y = y + 85

            if not m["transfers"]:
                draw.text(
                    (x + 25, row_y + 6),
                    "No transfers made",
                    fill=grey,
                    font=small_font
                )
            else:
                for t in m["transfers"]:

                    draw.rounded_rectangle(
                        (x + 15, row_y, x + col_w - 15, row_y + transfer_h - 4),
                        radius=8,
                        fill=row_bg
                    )

                    # OUT
                    draw.text(
                        (x + 28, row_y + 5),
                        t["out"][:14],
                        fill=red,
                        font=player_font
                    )
                    draw.text(
                        (x + 190, row_y + 8),
                        f"{t['out_points']} pts",
                        fill=grey,
                        font=small_font
                    )

                    # Arrow
                    draw.text(
                        (x + 275, row_y + 5),
                        "→",
                        fill=white,
                        font=player_font
                    )

                    # IN
                    draw.text(
                        (x + 325, row_y + 5),
                        t["in"][:14],
                        fill=green,
                        font=player_font
                    )
                    draw.text(
                        (x + 490, row_y + 8),
                        f"{t['in_points']} pts",
                        fill=grey,
                        font=small_font
                    )

                    # Star if the player in outscored the player out,
                    # clown if the player out scored more (hits only)
                    if t.get("verdict") == "star":
                        line_icon = get_star_icon(26)
                    elif t.get("verdict") == "clown":
                        line_icon = get_clown_icon(26)
                    else:
                        line_icon = None

                    if line_icon:
                        image.paste(
                            line_icon,
                            (x + col_w - 15 - 34, row_y + 2),
                            line_icon
                        )

                    row_y += transfer_h

            y += h + block_gap

    draw_column(40, left)
    draw_column(720, right)

    # Footer
    draw.text(
        (50, height - 115),
        f"Updated: {data['updated']}",
        fill=grey,
        font=small_font
    )
    draw.text(
        (50, height - 80),
        "Red = transferred out  •  Green = transferred in",
        fill=green,
        font=small_font
    )
    draw.text(
        (50, height - 45),
        "Per transfer: gold star = player in outscored player out "
        "(after the -4 on hits)  •  Clown = player out scored more  •  "
        "Score by name = season stars v clowns",
        fill=gold,
        font=small_font
    )

    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)

    return output


# ============================================================
# GW1 TEAM, NO TRANSFERS ("what if they never changed it")
# ============================================================

def get_player_positions():
    """Map player element IDs to position (1 GK, 2 DEF, 3 MID, 4 FWD)."""

    data = fpl_get("bootstrap-static/")

    return {
        element["id"]: element.get("element_type", 3)
        for element in data.get("elements", [])
    }


def get_live_stats(gameweek):
    """Map player element IDs to {points, minutes} for a Gameweek."""

    try:
        data = fpl_get(f"event/{gameweek}/live/")

        return {
            element["id"]: {
                "points": element.get("stats", {}).get("total_points", 0),
                "minutes": element.get("stats", {}).get("minutes", 0)
            }
            for element in data.get("elements", [])
        }
    except Exception:
        return {}


def load_gw1_cache():
    """Saved GW1-team results ({entry: {squad, gws}})."""

    if not os.path.exists(GW1_FILE):
        return {"entries": {}}

    try:
        with open(GW1_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        data.setdefault("entries", {})
        return data

    except Exception:
        return {"entries": {}}


def save_gw1_cache(cache):
    """Save results so finished gameweeks are only worked out once."""

    with open(GW1_FILE, "w", encoding="utf-8") as file:
        json.dump(cache, file, indent=2)


def score_fixed_team(squad, stats, positions):
    """Points the GW1 squad would score in one finished Gameweek.

    - Starting XI from GW1 (bench in GW1 order), no transfers, no chips
    - Captain scores double (vice-captain if the captain didn't play)
    - Automatic substitutions use the normal FPL rules
    """

    def minutes(element):
        return stats.get(element, {}).get("minutes", 0)

    def points(element):
        return stats.get(element, {}).get("points", 0)

    ordered = sorted(squad, key=lambda p: p["position"])

    xi = [p["element"] for p in ordered if p["position"] <= 11]
    bench = [p["element"] for p in ordered if p["position"] > 11]

    used = set()

    def formation_ok(players):
        counts = {1: 0, 2: 0, 3: 0, 4: 0}

        for element in players:
            counts[positions.get(element, 3)] += 1

        return counts[2] >= 3 and counts[3] >= 2 and counts[4] >= 1

    for starter in list(xi):

        if minutes(starter) > 0:
            continue

        for sub in bench:

            if sub in used or minutes(sub) == 0:
                continue

            starter_gk = positions.get(starter, 3) == 1
            sub_gk = positions.get(sub, 3) == 1

            if starter_gk != sub_gk:
                continue

            if not starter_gk:
                trial = [x for x in xi if x != starter] + [sub]

                if not formation_ok(trial):
                    continue

            xi[xi.index(starter)] = sub
            used.add(sub)
            break

    total = sum(points(element) for element in xi)

    captain = next((p["element"] for p in squad if p.get("is_captain")), None)
    vice = next((p["element"] for p in squad if p.get("is_vice_captain")), None)

    # Captain's points count twice (vice-captain if captain didn't play)
    if captain is not None and minutes(captain) > 0:
        total += points(captain)
    elif vice is not None and minutes(vice) > 0:
        total += points(vice)

    return total


def build_gw1_team_data(league_rows, finished_gw):
    """Running total of each manager's GW1 team, up to the last
    finished Gameweek, next to what they actually scored."""

    cache = load_gw1_cache()
    positions = get_player_positions()

    stats_cache = {}
    changed = False

    managers = []

    for row in league_rows:

        entry_id = row.get("entry")
        key = str(entry_id)

        entry_cache = cache["entries"].setdefault(key, {"gws": {}})

        # The GW1 squad (kept forever once fetched)
        if not entry_cache.get("squad"):

            picks = get_manager_picks(entry_id, 1).get("picks", [])

            if picks:
                entry_cache["squad"] = [
                    {
                        "element": p["element"],
                        "position": p["position"],
                        "is_captain": p.get("is_captain", False),
                        "is_vice_captain": p.get("is_vice_captain", False)
                    }
                    for p in picks
                ]
                changed = True

        squad = entry_cache.get("squad")

        by_gw = []
        running = []
        total = 0

        if squad:

            for gw in range(1, finished_gw + 1):

                saved = entry_cache["gws"].get(str(gw))

                if saved is None:

                    if gw not in stats_cache:
                        stats_cache[gw] = get_live_stats(gw)

                    if not stats_cache[gw]:
                        break    # FPL didn't answer - try again next time

                    saved = score_fixed_team(squad, stats_cache[gw], positions)

                    entry_cache["gws"][str(gw)] = saved
                    changed = True

                total += saved
                by_gw.append(saved)
                running.append(total)

        # What they actually scored (after hits, chips and all)
        current = (row.get("history") or {}).get("current", [])

        actual = None

        for g in current:
            if g.get("event") == finished_gw:
                actual = g.get("total_points")

        if actual is None and current:
            actual = current[-1].get("total_points")

        managers.append({
            "entry": entry_id,
            "manager": row.get("player_name", "Unknown Manager"),
            "team": row.get("entry_name", "Unknown Team"),
            "has_team": bool(squad),
            "by_gw": by_gw,
            "running": running,
            "total": total if squad else None,
            "actual": actual
        })

    if changed:
        try:
            save_gw1_cache(cache)
        except Exception:
            pass

    managers.sort(
        key=lambda m: (
            m["total"] is None,
            -(m["total"] or 0),
            m["manager"]
        )
    )

    return {
        "finished_gw": finished_gw,
        "managers": managers
    }


# ============================================================
# CURRY TABLE (every gameweek, period winners, who owes a curry)
# ============================================================

def build_curry_table():
    """Every manager's points for every gameweek so far, plus the result
    of each 5-week period (who won the curry and who owes one),
    including backdated periods from before the game started."""

    league_name, standings = get_league()

    finished_gw = get_current_gameweek()
    live_gw = get_live_gameweek()

    rows, state = get_league_rows(standings, finished_gw, live_gw)

    by_row = {r["entry"]: r for r in rows}
    histories = {e: r["history"] for e, r in by_row.items()}

    swaps = state.get("swaps", [])

    # Who was in which league DURING each period: start from today's
    # leagues and undo the swaps one by one, newest first.
    work = dict(state["assignments"])
    before = [None] * len(swaps)

    for k in range(len(swaps) - 1, -1, -1):
        for e in swaps[k].get("relegated", []):
            work[str(e)] = "championship"
        for e in swaps[k].get("promoted", []):
            work[str(e)] = "challenger"
        before[k] = dict(work)

    # Leagues as they were when first locked in (used for backdating)
    initial = dict(work)

    label = {
        "championship": "Championship",
        "challenger": "Challenger",
        "all": "Single league"
    }

    # Emoji that shows WHICH league a winner / loser belongs to, so you can
    # see who owes whom: a loser only owes the winner with the same emoji.
    emoji = {
        "championship": "🏆",
        "challenger": "⚔️",
        "all": "⚽"
    }

    won = {e: 0 for e in by_row}
    owed = {e: 0 for e in by_row}

    def pack(e, pts, place):
        return {
            "entry": e,
            "manager": by_row[e].get("player_name", "Unknown"),
            "points": pts,
            "place": place
        }

    def settle(text, number, first, last, membership, saved_winners,
               backdated, single=False):
        """Work out one finished period: winner and who owes a curry.

        single=True means everybody was in ONE league (before the split)."""

        begin = basis_start(first)

        period = {
            "label": text,
            "number": number,
            "first": first,
            "last": last,
            "complete": True,
            "backdated": backdated,
            "single": single,
            "by_entry": {},
            "leagues": []
        }

        for league in (["all"] if single else ["championship", "challenger"]):

            if single:
                members = list(by_row)
            else:
                members = [
                    e for e in by_row if membership.get(str(e)) == league
                ]

            if not members:
                continue

            winners = [e for e in saved_winners if e in members]

            def pts_of(e):
                return points_between(histories[e], begin, last)

            # A saved winner always comes first, so this matches the
            # curry icon on the main dashboard
            ranked = sorted(
                members,
                key=lambda e: (
                    0 if e in winners else 1,
                    -pts_of(e),
                    -cumulative_points(histories[e], last)
                )
            )

            winner = ranked[0]
            owers = []

            if len(ranked) >= 2:
                owers = ranked[1:] if CURRY_OWED_BY == "all" else [ranked[-1]]

            won[winner] += 1

            for e in owers:
                owed[e] += 1

            for place, e in enumerate(ranked, start=1):
                period["by_entry"][str(e)] = {
                    "points": pts_of(e),
                    "league": league,
                    "emoji": emoji[league],
                    "place": place,
                    "status": (
                        "won" if e == winner
                        else "owes" if e in owers
                        else ""
                    )
                }

            period["leagues"].append({
                "league": league,
                "label": label[league],
                "emoji": emoji[league],
                "winner": pack(winner, pts_of(winner), 1),
                "owes": [pack(e, pts_of(e), ranked.index(e) + 1)
                         for e in owers]
            })

        return period

    periods = []

    # ---- backdated periods (before the game started) ----
    if BACKDATE_CURRIES:

        blocks = []
        last = state["start_gw"] - 1

        while last - PERIOD_LENGTH + 1 >= 1 and last <= finished_gw:
            blocks.append((last - PERIOD_LENGTH + 1, last))
            last -= PERIOD_LENGTH

        blocks.reverse()

        for number, (first, last) in enumerate(blocks, start=1):
            periods.append(
                settle(
                    f"B{number}", number, first, last, initial, [], True,
                    single=(last <= SINGLE_LEAGUE_UNTIL_GW)
                )
            )

    # ---- finished periods ----
    for k, swap in enumerate(swaps):

        first, last = swap["gameweeks"]

        periods.append(
            settle(
                f"P{swap['period']}", swap["period"], first, last,
                before[k], swap.get("winners", []), False
            )
        )

    # ---- the period being played now ----
    first, last = period_bounds(state["start_gw"], state["periods_done"])

    if first <= live_gw:

        period = {
            "label": f"P{state['periods_done'] + 1}",
            "number": state["periods_done"] + 1,
            "first": first,
            "last": last,
            "complete": False,
            "backdated": False,
            "by_entry": {},
            "leagues": []
        }

        for league in ("championship", "challenger"):

            group = sorted(
                [r for r in rows if r["league"] == league],
                key=lambda r: r["league_rank"]
            )

            if not group:
                continue

            for r in group:
                period["by_entry"][str(r["entry"])] = {
                    "points": r["points"],
                    "league": league,
                    "emoji": emoji[league],
                    "place": r["league_rank"],
                    "status": ""
                }

            period["leagues"].append({
                "league": league,
                "label": label[league],
                "emoji": emoji[league],
                "winner": pack(group[0]["entry"], group[0]["points"], 1),
                "owes": []
            })

        periods.append(period)

    # ---- every manager, every gameweek ----
    managers = []

    for r in rows:

        gws = {}

        for g in r["history"].get("current", []):
            event = g.get("event")

            if event and event <= live_gw:
                gws[str(event)] = (
                    g.get("points", 0) - g.get("event_transfers_cost", 0)
                )

        # The gameweek being shown uses the same live figure as the
        # main dashboard
        if live_gw:
            gws[str(live_gw)] = r["gw_points"]

        managers.append({
            "entry": r["entry"],
            "manager": r.get("player_name", "Unknown Manager"),
            "team": r.get("entry_name", "Unknown Team"),
            "league": r["league"],
            "rank": r["league_rank"],
            "gws": gws,
            "curries_won": won.get(r["entry"], 0),
            "curries_owed": owed.get(r["entry"], 0)
        })

    return {
        "league_name": league_name,
        "shown_gw": live_gw,
        "live": live_gw > finished_gw,
        "start_gw": state["start_gw"],
        "owed_by": CURRY_OWED_BY,
        "single_until": SINGLE_LEAGUE_UNTIL_GW,
        "updated": datetime.now().strftime("%d %b %Y %H:%M"),
        "managers": managers,
        "periods": periods
    }


@app.route("/api/curry-table")
def api_curry_table():

    try:
        return jsonify(build_curry_table())

    except Exception as error:

        return jsonify({"error": str(error)}), 500


# ============================================================
# CHIP PLANNER (marquee)
# Works out the best week for each chip from the live FPL fixtures.
#   * First half  : next unfinished GW up to GW19 (CHIP_SET_SPLIT_GW)
#   * Second half : GW20-38, switches on automatically after GW19
#   * Doubles and blanks are read from the live fixture list each time
# ============================================================

CHIP_PLAN_TTL = 30 * 60      # seconds between refreshes from FPL

_chip_plan_cache = {"t": 0.0, "data": None}
_chip_plan_lock = threading.Lock()


def _cp_mean(vals):
    vals = list(vals)
    return sum(vals) / len(vals) if vals else 0.0


def _cp_top_mean(vals, n):
    return _cp_mean(sorted(vals, reverse=True)[:n])


def build_chip_plan(bootstrap, fixtures):
    """Pure function: returns {status, half, window, picks, items}."""

    events = bootstrap["events"]
    teams = {t["id"]: t["short_name"] for t in bootstrap["teams"]}
    last_gw = max(e["id"] for e in events)

    unfinished = [e["id"] for e in events if not e.get("finished")]

    if not unfinished:
        return {"status": "season_over",
                "items": ["🏁 Season complete - see you next year!"]}

    start = min(unfinished)

    if start <= CHIP_SET_SPLIT_GW:
        half, last = 1, CHIP_SET_SPLIT_GW
    else:
        half, last = 2, last_gw

    window = list(range(start, last + 1))

    # fx[gw][team] = [(opponent, "H"/"A", ease 1-5)]  (5 = easiest)
    fx = {g: {t: [] for t in teams} for g in range(1, last_gw + 1)}
    scheduled = {g: 0 for g in range(1, last_gw + 1)}

    for f in fixtures:
        gw = f.get("event")

        if not gw or gw not in fx:
            continue

        h, a = f["team_h"], f["team_a"]

        fx[gw][h].append((teams[a], "H", 6 - f["team_h_difficulty"]))
        fx[gw][a].append((teams[h], "A", 6 - f["team_a_difficulty"]))
        scheduled[gw] += 1

    def score(gw, t):
        return sum(x[2] for x in fx[gw][t])

    # only judge gameweeks whose fixtures have been published
    window = [g for g in window if scheduled[g] > 0]

    if not window:
        return {"status": "no_fixtures", "half": half,
                "items": ["📅 Fixtures for the next gameweeks aren't published yet"]}

    doubles = {g: [teams[t] for t in teams if len(fx[g][t]) >= 2] for g in window}
    blanks = {g: [teams[t] for t in teams if len(fx[g][t]) == 0] for g in window}

    fh, bb, tc, wc = {}, {}, {}, {}
    why = {"fh": {}, "bb": {}, "tc": {}, "wc": {}}

    attackers = sorted(
        [p for p in bootstrap["elements"] if p["element_type"] in (3, 4)],
        key=lambda p: p.get("total_points", 0),
        reverse=True
    )[:20]

    for g in window:

        scores = [score(g, t) for t in teams]
        nb, nd = len(blanks[g]), len(doubles[g])

        # Free Hit: blanks first, then doubles, then best-v-average fixtures
        fh[g] = 10 * nb + 3 * nd + (_cp_top_mean(scores, 8) - _cp_mean(scores))

        if nb:
            why["fh"][g] = (
                f"{nb} team{'s' if nb != 1 else ''} blank "
                f"({', '.join(blanks[g][:6])})"
            )
        elif nd:
            why["fh"][g] = (
                f"{nd} team{'s' if nd != 1 else ''} double "
                f"({', '.join(doubles[g][:6])})"
            )
        else:
            why["fh"][g] = "biggest gap between best and average fixtures"

        # Bench Boost: doubles first, then fixture strength across a squad
        extra = sum(max(0, len(fx[g][t]) - 1) for t in teams)
        bb[g] = 10 * extra + _cp_top_mean(scores, 11)

        why["bb"][g] = (
            f"{nd} double{'s' if nd != 1 else ''} ({', '.join(doubles[g][:6])})"
            if nd else "strong fixtures across the board"
        )

        # Triple Captain: best form-weighted fixture among top attackers
        best = None

        for p in attackers:
            s = score(g, p["team"])
            val = s * float(p.get("points_per_game") or 0)

            if best is None or val > best[0]:
                best = (val, p, s)

        if best:
            val, p, s = best
            opps = " + ".join(
                f"{o}({ha})" for o, ha, _ in fx[g][p["team"]]
            ) or "blank"

            tc[g] = val
            why["tc"][g] = f"{p['web_name']} ({teams[p['team']]}) vs {opps}"

    # Wildcard: size of the fixture swing over the next 4 gameweeks
    def run_mean(t, lo, hi):
        vals = [
            x[2]
            for g in range(max(1, lo), min(last_gw, hi) + 1)
            for x in fx[g][t]
        ]
        return _cp_mean(vals) if vals else None

    for g in window:

        swings = []

        for t in teams:
            before = run_mean(t, g - 4, g - 1)
            after = run_mean(t, g, g + 3)

            if before is not None and after is not None:
                swings.append((after - before, teams[t]))

        if not swings:
            continue

        swings.sort(reverse=True)
        ups, downs = swings[:5], swings[-5:]

        disrupted = sum(
            len(doubles.get(x, [])) + len(blanks.get(x, []))
            for x in (g, g + 1, g + 2)
        )

        wc[g] = (
            sum(s for s, _ in ups)
            + sum(-s for s, _ in downs)
            + 3 * disrupted
        )

        good = [n for s, n in ups[:3] if s > 0]

        why["wc"][g] = (
            f"{', '.join(good)} move onto good fixtures"
            if good else "squad rebuild around the fixture run"
        )

    chips = {"fh": fh, "wc": wc, "bb": bb, "tc": tc}

    # Only one chip can be played per gameweek: choose the best combination
    # of different gameweeks.
    ranked = {}

    for c, d in chips.items():
        mx = max(d.values()) if d and max(d.values()) > 0 else 1
        ranked[c] = sorted(
            ((v / mx, g) for g, v in d.items()), reverse=True
        )[:4]

    names = list(chips)
    best_combo, best_total = None, -1

    for combo in itertools.product(*(ranked[c] or [(0, None)] for c in names)):
        gws = [g for _, g in combo if g is not None]

        if len(window) >= 4 and len(set(gws)) != len(gws):
            continue

        total = sum(s for s, _ in combo)

        if total > best_total:
            best_combo, best_total = combo, total

    pick = (
        {c: best_combo[i][1] for i, c in enumerate(names)}
        if best_combo else {}
    )

    def alt(c):
        return [g for _, g in ranked[c] if g != pick.get(c)][:2]

    half_label = "1st half" if half == 1 else "2nd half"

    items = [
        f"📅 CHIP PLANNER - {half_label} "
        f"(GW{window[0]}-GW{window[-1]}), updates itself from live fixtures"
    ]

    label = {
        "wc": "🔄 WILDCARD",
        "fh": "🎯 FREE HIT",
        "bb": "🪑 BENCH BOOST",
        "tc": "👑 TRIPLE CAPTAIN"
    }

    for c in ("wc", "fh", "bb", "tc"):
        g = pick.get(c)

        if g is None:
            continue

        a = alt(c)
        also = (
            f" (also: {', '.join('GW' + str(x) for x in a)})" if a else ""
        )

        items.append(f"{label[c]}: GW{g} - {why[c].get(g, '')}{also}")

    for g in window:
        if doubles[g]:
            items.append(f"⚡ DOUBLE GW{g}: {', '.join(doubles[g])}")
        if blanks[g]:
            items.append(f"🚫 BLANK GW{g}: {', '.join(blanks[g])}")

    if half == 1:
        items.append(
            f"⏰ First-set chips expire at the GW{CHIP_SET_SPLIT_GW} "
            "deadline - use them or lose them"
        )

    return {
        "status": "ok",
        "half": half,
        "window": [window[0], window[-1]],
        "picks": pick,
        "items": items
    }


def get_chip_plan(force=False):
    """Fetch + cache. Keeps the last good result if FPL can't be reached."""

    with _chip_plan_lock:

        fresh = (
            _chip_plan_cache["data"] is not None
            and time.time() - _chip_plan_cache["t"] < CHIP_PLAN_TTL
        )

        if fresh and not force:
            return _chip_plan_cache["data"]

        try:
            data = build_chip_plan(
                fpl_get("bootstrap-static/"),
                fpl_get("fixtures/")
            )
            _chip_plan_cache["t"] = time.time()
            _chip_plan_cache["data"] = data
            return data

        except Exception as error:

            if _chip_plan_cache["data"] is not None:
                return _chip_plan_cache["data"]

            return {
                "status": "error",
                "items": ["⚠️ Chip planner can't reach FPL right now"],
                "error": str(error)
            }


@app.route("/api/chip-planner")
def api_chip_planner():
    return jsonify(get_chip_plan())


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/current")
def api_current():

    try:

        snapshot = build_snapshot()

        save_snapshot(snapshot)

        return jsonify(snapshot)

    except Exception as error:

        return jsonify({
            "error": str(error)
        }), 500


@app.route("/api/history")
def api_history():

    return jsonify(
        load_history()
    )

@app.route("/api/hits")
def api_hits():
    """The hits record (every transfer hit taken this season)."""

    from_saved = False

    try:
        league_name, standings = get_league()
        gameweek = get_current_gameweek()
        rows, _ = get_league_rows(standings, gameweek)
        data = record_hits(rows)

    except Exception:
        # FPL not reachable - show what was saved earlier
        data = load_hits()
        from_saved = True

    managers = sorted(
        data["managers"].values(),
        key=lambda m: (-m["total_points"], m["manager"])
    )

    return jsonify({
        "updated": data.get("updated"),
        "from_saved": from_saved,
        "total_points": sum(m["total_points"] for m in managers),
        "total_hits": sum(m["total_hits"] for m in managers),
        "managers": managers
    })


@app.route("/api/deadline")
def api_deadline():
    """The next FPL deadline (for the live countdown)."""

    try:
        data = fpl_get("bootstrap-static/")

        now = datetime.now(timezone.utc)

        upcoming = []

        for event in data.get("events", []):

            deadline = event.get("deadline_time")

            if not deadline:
                continue

            deadline_dt = datetime.fromisoformat(
                deadline.replace("Z", "+00:00")
            )

            if deadline_dt > now:
                upcoming.append((deadline_dt, event))

        if not upcoming:
            return jsonify({"deadline": None})

        deadline_dt, event = min(upcoming, key=lambda item: item[0])

        return jsonify({
            "gameweek": event.get("id"),
            "name": event.get("name"),
            "deadline": deadline_dt.isoformat().replace("+00:00", "Z")
        })

    except Exception as error:

        return jsonify({
            "error": str(error)
        }), 500


@app.route("/api/gw1-team")
def api_gw1_team():
    """Running total if everyone had kept their Gameweek 1 team."""

    try:
        league_name, standings = get_league()
        finished_gw = get_current_gameweek()

        rows, _ = get_league_rows(standings, finished_gw)

        return jsonify(build_gw1_team_data(rows, finished_gw))

    except Exception as error:

        return jsonify({
            "error": str(error)
        }), 500


@app.route("/download-whatsapp")
def download_whatsapp():

    try:

        snapshot = build_snapshot()

        # Save the latest snapshot
        save_snapshot(snapshot)

        graphic = create_whatsapp_graphic(
            snapshot
        )

        filename = (
            f"FPL_Gameweek_"
            f"{snapshot['gameweek']}.png"
        )

        return send_file(
            graphic,
            mimetype="image/png",
            as_attachment=True,
            download_name=filename
        )

    except Exception as error:

        return (
            f"Unable to create graphic: {error}",
            500
        )


@app.route("/download-transfers")
def download_transfers():

    try:

        # Defaults to the live Gameweek (same as the dashboard).
        # Use /download-transfers?gw=12 to pick a specific one.
        gameweek = request.args.get("gw", type=int)

        if not gameweek:
            gameweek = get_live_gameweek()

        data = build_transfers_data(gameweek)

        graphic = create_transfers_graphic(data)

        return send_file(
            graphic,
            mimetype="image/png",
            as_attachment=True,
            download_name=f"FPL_Transfers_GW{gameweek}.png"
        )

    except Exception as error:

        return (
            f"Unable to create transfers graphic: {error}",
            500
        )

# ============================================================
# HTML / CSS / JAVASCRIPT
# ============================================================

HTML = r"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1.0">

<title>FPL League Dashboard</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    font-family:
        Arial,
        Helvetica,
        sans-serif;

    background:
        linear-gradient(
            135deg,
            #061a14,
            #08251c,
            #02110d
        );

    color: #FFFFFF;

    min-height: 100vh;
}


/* ==========================================================
   HEADER
   ========================================================== */

.header {

    padding: 30px 20px 20px;

    text-align: center;

    background:
        linear-gradient(
            90deg,
            #00ff87,
            #00c46a
        );

    color: #061a14;

    box-shadow:
        0 5px 25px rgba(
            0,
            255,
            135,
            0.25
        );
}

.header h1 {

    margin: 0;

    font-size: 38px;

    font-weight: 900;

    letter-spacing: 1px;
}

.header p {

    margin: 8px 0 0;

    font-size: 16px;

    font-weight: bold;
}


/* ==========================================================
   CONTROLS
   ========================================================== */

.deadline-box {

    display: inline-block;

    margin-top: 14px;

    padding: 10px 22px;

    border-radius: 14px;

    background: #061a14;

    color: #00ff87;

    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.25);

    cursor: pointer;
}

.deadline-label {

    font-size: 12px;

    font-weight: 800;

    letter-spacing: 1.5px;

    color: #a9c5b9;
}

.deadline-time {

    font-size: 30px;

    font-weight: 900;

    font-variant-numeric: tabular-nums;
}

.deadline-when {

    font-size: 12px;

    color: #a9c5b9;
}

.deadline-soon .deadline-time {
    color: #ffd700;
}

.deadline-urgent .deadline-time {
    color: #ff4d6d;
}

.controls {

    max-width: 1400px;

    margin: 20px auto;

    padding: 0 20px;

    display: flex;

    justify-content: space-between;

    align-items: center;

    gap: 15px;

    flex-wrap: wrap;
}

button {

    border: 0;

    border-radius: 10px;

    padding: 12px 20px;

    background: #00ff87;

    color: #04120d;

    font-weight: 800;

    cursor: pointer;

    transition: 0.2s;
}

button:hover {

    transform: translateY(-2px);

    box-shadow:
        0 5px 20px rgba(
            0,
            255,
            135,
            0.25
        );
}

#status {

    color: #a9c5b9;

    font-size: 14px;
}


/* ==========================================================
   MAIN
   ========================================================== */

.container {

    max-width: 1400px;

    margin: auto;

    padding: 0 20px 50px;
}


/* ==========================================================
   LEAGUE GRID
   ========================================================== */

.leagues {

    display: grid;

    grid-template-columns:
        repeat(
            2,
            minmax(
                0,
                1fr
            )
        );

    gap: 25px;
}

@media(max-width: 900px) {

    .leagues {

        grid-template-columns: 1fr;
    }

}


/* ==========================================================
   LEAGUE CARD
   ========================================================== */

.league {

    background:
        rgba(
            8,
            31,
            24,
            0.95
        );

    border:
        1px solid
        rgba(
            0,
            255,
            135,
            0.18
        );

    border-radius: 18px;

    overflow: hidden;

    box-shadow:
        0 15px 50px
        rgba(
            0,
            0,
            0,
            0.35
        );
}

.league-header {

    padding: 20px;

    background:
        linear-gradient(
            135deg,
            #0a3c2a,
            #071e17
        );

    border-bottom:
        1px solid
        rgba(
            0,
            255,
            135,
            0.15
        );
}

.league-header h2 {

    margin: 0;

    font-size: 24px;
}

.league-header span {

    color: #8da99c;

    font-size: 13px;
}


/* ==========================================================
   PLAYER ROW
   ========================================================== */

.player {

    display: grid;

    grid-template-columns:
        55px
        1fr
        90px
        95px;

    align-items: center;

    padding: 15px;

    border-bottom:
        1px solid
        rgba(
            255,
            255,
            255,
            0.06
        );

    transition: 0.2s;
}

.player:hover {

    background:
        rgba(
            0,
            255,
            135,
            0.06
        );
}

.position {

    font-size: 22px;

    font-weight: 900;

    text-align: center;
}

.rank1 {

    color: #ffd700;
}

.rank2 {

    color: #c0c0c0;
}

.rank3 {

    color: #cd7f32;
}

.player-name {

    font-weight: 800;

    font-size: 16px;
}

.team-name {

    margin-top: 4px;

    color: #76958a;

    font-size: 12px;
}

.total {

    text-align: right;

    font-size: 21px;

    font-weight: 900;

    color: #FFFFFF;
}

.gw {

    text-align: right;

    font-size: 14px;

    color: #00ff87;

    font-weight: bold;
}

.movement {

    font-size: 12px;

    margin-left: 6px;
}

.up {

    color: #00ff87;
}

.down {

    color: #ff4d6d;
}

.same {

    color: #728a80;
}


/* ==========================================================
   CHIPS
   ========================================================== */

.chips {

    display: flex;

    gap: 4px;

    margin-top: 6px;
}

.chip {

    font-size: 10px;

    font-weight: 800;

    padding: 2px 6px;

    border-radius: 6px;

    cursor: default;
}

.chip.avail {

    background: rgba(0, 255, 135, 0.15);

    color: #00ff87;

    border: 1px solid rgba(0, 255, 135, 0.4);
}

.chip.used {

    background: transparent;

    color: #5d776c;

    text-decoration: line-through;

    border: 1px solid rgba(255, 255, 255, 0.1);
}

.chip-note {

    color: #76958a;

    font-size: 12px;

    margin: 0 0 12px;
}


/* ==========================================================
   CAPTAINS
   ========================================================== */

.chart-card {

    margin-top: 25px;

    background:
        rgba(
            8,
            31,
            24,
            0.95
        );

    border:
        1px solid
        rgba(
            0,
            255,
            135,
            0.18
        );

    border-radius: 18px;

    padding: 25px;
}

.chart-card h2 {

    margin-top: 0;
}

.captains-grid {

    display: grid;

    grid-template-columns:
        repeat(
            auto-fill,
            minmax(260px, 1fr)
        );

    gap: 15px;
}

.captain-card {

    background: #0D3024;

    border-radius: 12px;

    padding: 15px;
}

.captain-manager {

    font-weight: 800;

    font-size: 16px;
}

.captain-team {

    color: #76958a;

    font-size: 12px;

    margin-top: 4px;
}

.captain-name {

    color: #00ff87;

    font-weight: 800;

    margin-top: 12px;
}

.captain-points {

    font-size: 22px;

    font-weight: 900;

    margin-top: 4px;
}


/* ==========================================================
   HITS TRACKER (pop-up)
   ========================================================== */

.modal {

    display: none;

    position: fixed;

    top: 0;
    left: 0;
    right: 0;
    bottom: 0;

    background: rgba(0, 0, 0, 0.75);

    align-items: center;

    justify-content: center;

    z-index: 50;

    padding: 20px;
}

.modal-box {

    background: #081f18;

    border: 1px solid rgba(0, 255, 135, 0.25);

    border-radius: 18px;

    width: 100%;

    max-width: 780px;

    max-height: 90vh;

    overflow: auto;

    padding: 25px;
}

.modal-top {

    display: flex;

    justify-content: space-between;

    align-items: center;

    margin-bottom: 15px;
}

.modal-top h2 {

    margin: 0;
}

.close-btn {

    padding: 6px 12px;

    background: #0D3024;

    color: #FFFFFF;
}

.modal-box input {

    width: 100%;

    padding: 12px;

    margin: 10px 0;

    border-radius: 10px;

    border: 1px solid rgba(0, 255, 135, 0.3);

    background: #0D3024;

    color: #FFFFFF;

    font-size: 16px;
}

.hits-error {

    color: #ff4d6d;

    margin-top: 10px;

    font-size: 14px;
}

.hits-summary {

    background: #0D3024;

    border-radius: 12px;

    padding: 15px;

    margin-bottom: 15px;
}

.hits-summary strong {

    color: #ff4d6d;

    font-size: 22px;
}

.hits-table {

    width: 100%;

    border-collapse: collapse;
}

.hits-table th {

    text-align: left;

    color: #76958a;

    font-size: 12px;

    padding: 8px;
}

.hits-table td {

    padding: 10px 8px;

    border-top: 1px solid rgba(255, 255, 255, 0.06);

    vertical-align: top;

    font-size: 14px;
}

.hits-points {

    color: #ff4d6d;

    font-weight: 900;

    font-size: 18px;
}

.hits-weeks {

    color: #a9c5b9;

    font-size: 12px;
}


/* ==========================================================
   CURRY TABLE
   ========================================================== */

.curry-table th,
.curry-table td {

    text-align: center;

    padding: 8px 6px;

    white-space: nowrap;
}

.curry-table th:nth-child(2),
.curry-table td:nth-child(2) {

    text-align: left;
}

.curry-period {

    background: rgba(0, 255, 135, 0.07);

    font-weight: 800;

    border-left: 1px solid rgba(0, 255, 135, 0.3);
}

.curry-won {

    background: rgba(255, 215, 0, 0.2);

    color: #ffd700;

    font-weight: 900;
}

.curry-owes {

    background: rgba(255, 77, 109, 0.2);

    color: #ff4d6d;

    font-weight: 900;
}

.curry-sub {

    font-size: 10px;

    font-weight: 800;
}


/* ==========================================================
   FOOTER
   ========================================================== */

.footer {

    text-align: center;

    color: #557268;

    font-size: 12px;

    padding: 20px;
}


/* ==========================================================
   CHIP PLANNER MARQUEE
   ========================================================== */

.chip-marquee {

    overflow: hidden;

    white-space: nowrap;

    padding: 10px 0;

    background: #0a241b;

    border-bottom: 2px solid rgba(0, 255, 135, 0.35);

    font-weight: 800;

    font-size: 16px;

    color: #FFFFFF;
}

.chip-marquee-track {

    display: inline-block;

    padding-left: 100%;

    animation: chip-scroll 60s linear infinite;
}

.chip-marquee:hover .chip-marquee-track {
    animation-play-state: paused;
}

.chip-marquee-item {

    margin-right: 60px;
}

.chip-marquee-item.alert {

    color: #ffd700;
}

@keyframes chip-scroll {

    from {
        transform: translateX(0);
    }

    to {
        transform: translateX(-100%);
    }
}

@media (prefers-reduced-motion: reduce) {

    .chip-marquee {
        overflow-x: auto;
    }

    .chip-marquee-track {
        padding-left: 20px;
        animation: none;
    }
}

</style>

</head>


<body>


<div class="header">

    <h1>⚽ FPL LEAGUE</h1>

    <p id="leagueName">
        Loading league...
    </p>

    <div id="deadlineBox" class="deadline-box" style="display: none;" onclick="downloadDeadline()" title="Click to save as a PNG">

        <div class="deadline-label" id="deadlineLabel"></div>

        <div class="deadline-time" id="deadlineTime"></div>

        <div class="deadline-when" id="deadlineWhen"></div>

    </div>

</div>

<div class="chip-marquee" id="chipMarquee">
    <div class="chip-marquee-track" id="chipMarqueeTrack">
        <span class="chip-marquee-item">Loading chip planner...</span>
    </div>
</div>

<div class="controls">

    <div id="status">
        Connecting to Fantasy Premier League...
    </div>

    <button onclick="refreshData()">
        🔄 UPDATE FROM FPL
    </button>

    <button onclick="downloadWhatsApp()">
    📲 WHATSAPP GRAPHIC
</button>

    <button onclick="downloadTransfers()">
    🔁 TRANSFERS GRAPHIC
</button>

    <button onclick="openHits()">
    🤡 HITS TRACKER
</button>

</div>


<div class="container">

    <div id="chipNote" class="chip-note"></div>

    <div class="leagues">

        <div class="league">

            <div class="league-header">

                <h2>
                    🏆 TOP 5
                </h2>

                <span>
                    Championship League
                </span>

            </div>

            <div id="top5"></div>

        </div>


        <div class="league">

            <div class="league-header">

                <h2>
                    ⚔️ 6TH — 10TH
                </h2>

                <span>
                    Challenger League
                </span>

            </div>

            <div id="bottom5"></div>

        </div>

    </div>


    <div class="chart-card">

        <div style="display: flex; justify-content: space-between;
                    align-items: center; flex-wrap: wrap; gap: 10px;
                    margin-bottom: 15px;">

            <h2 style="margin: 0;">
                🧢 Captains This Gameweek
            </h2>

            <button onclick="downloadCaptainsPng()">
                📸 SAVE CAPTAINS AS PNG
            </button>

        </div>

        <div id="captains" class="captains-grid"></div>

    </div>


    <div class="chart-card">

        <h2>
            🪑 Points Left on the Bench
        </h2>

        <div id="benchNote" class="chip-note"></div>

        <div id="bench"></div>

    </div>


    <div class="chart-card">

        <h2>
            🃏 Chip Scores
        </h2>

        <div class="chip-note">
            Wildcard, Free Hit and Bench Boost show the whole team's points
            for that gameweek. Triple Captain shows only the captained
            player's points (including the x3).
        </div>

        <div id="chipScores" style="overflow-x: auto;"></div>

    </div>


    <div class="chart-card">

        <div style="display: flex; justify-content: space-between;
                    align-items: center; flex-wrap: wrap; gap: 10px;
                    margin-bottom: 15px;">

            <h2 style="margin: 0;">
                🧊 Frozen GW1 Team (No Transfers)
            </h2>

            <button onclick="downloadGw1Png()">
                📸 SAVE FROZEN TEAM AS PNG
            </button>

        </div>

        <div class="chip-note">
            Running total if each manager had kept their Gameweek 1 squad
            all season: same starting XI, bench order and captain, no
            transfers, no hits and no chips. Auto-subs and the
            vice-captain rule still apply. Counted once each gameweek has
            finished. "Transfers effect" is actual total minus frozen
            total, so green means their changes paid off.
        </div>

        <div id="gw1Team" style="overflow-x: auto;"></div>

    </div>


    <div class="chart-card">

        <div style="display: flex; justify-content: space-between;
                    align-items: center; flex-wrap: wrap; gap: 10px;
                    margin-bottom: 15px;">

            <h2 style="margin: 0;">
                🍛 Curry Table
            </h2>

            <button onclick="downloadCurryPng()">
                📸 SAVE CURRY TABLE AS PNG
            </button>

        </div>

        <div id="curryNote" class="chip-note"></div>

        <div id="curryTable" style="overflow-x: auto;"></div>

        <h3 style="margin: 25px 0 10px;">Curry results by period</h3>

        <div id="curryResults" style="overflow-x: auto;"></div>

    </div>


</div>


<div id="hitsModal" class="modal"
     onclick="if (event.target === this) closeHits()">

    <div class="modal-box">

        <div class="modal-top">

            <h2>🤡 Hits Tracker</h2>

            <button class="close-btn" onclick="closeHits()">✕</button>

        </div>

        <div id="hitsResults"></div>

    </div>

</div>


<div class="footer">

    FPL League Dashboard • Automatically updated from FPL •
    <a href="/api/debug-scores" style="color: #557268;">Score check</a>

</div>


<script>

let currentData = null;
let lastHitsData = null;

// "+12" for a normal score, "-4" (no plus sign) when it's negative
function gwSigned(n) {
    return n < 0 ? String(n) : "+" + n;
}


/* ==========================================================
   LOAD CURRENT DATA
   ========================================================== */

async function refreshData() {

    const status =
        document.getElementById(
            "status"
        );

    status.innerText =
        "Updating from FPL...";

    try {

        const response =
            await fetch(
                "/api/current"
            );

        const data =
            await response.json();

        if (data.error) {

            throw new Error(
                data.error
            );
        }

        currentData = data;

        renderDashboard(
            data
        );

        status.innerText =
            `Updated GW${data.gameweek} • ${data.updated}`;

    } catch(error) {

        console.error(error);

        status.innerText =
            "Unable to update FPL data: "
            + error.message;
    }
}

function downloadWhatsApp() {

    if (!currentData) {

        alert(
            "Please update the FPL data first."
        );

        return;
    }

    window.location.href =
        "/download-whatsapp";
}

function downloadTransfers() {

    window.location.href =
        "/download-transfers";
}


/* ==========================================================
   RENDER DASHBOARD
   ========================================================== */

function renderDashboard(data) {

    document.getElementById(
        "leagueName"
    ).innerText =
        `${data.league_name} • GAMEWEEK ${data.gameweek}`
        + ` • PERIOD ${data.period.number}`
        + ` (GW ${data.period.first_gw}-${data.period.last_gw})`;


    const top5 =
        data.players.filter(
            p => p.league === "championship"
        );

    const bottom5 =
        data.players.filter(
            p => p.league === "challenger"
        );


    document.getElementById(
        "chipNote"
    ).innerText =
        data.chip_half === 1
            ? `Chips: green = available, struck through = used. `
              + `First-set chips expire after the GW${data.chip_split_gw} deadline.`
            : `Chips: green = available, struck through = used. `
              + `Second-set chips (GW${data.chip_split_gw + 1}-38).`;


    document.getElementById(
        "chipNote"
    ).innerText +=
        " 🤡 = transfer points hit taken this gameweek.";


    renderPlayers(
        "top5",
        top5
    );

    renderPlayers(
        "bottom5",
        bottom5
    );


    renderCaptains(
        data.players
    );

    renderBench(
        data
    );

    renderChipScores(
        data.players
    );

    loadGw1Team();
    loadCurryTable();
}


/* ==========================================================
   RENDER PLAYER ROWS
   ========================================================== */

function renderPlayers(
    elementId,
    players
) {

    const container =
        document.getElementById(
            elementId
        );

    container.innerHTML = "";

    const periodLabel =
        (currentData && currentData.period && currentData.period.reset)
            ? "period"
            : "total";

    const gwLabel =
        currentData
            ? `GW${currentData.gameweek}` + (currentData.live ? " LIVE" : "")
            : "GW";


    players.forEach(
        player => {

            const chipsHtml =
                (player.chips || []).map(chip => {

                    const cls =
                        chip.available ? "avail" : "used";

                    const tip =
                        chip.available
                            ? `${chip.name}: available`
                            : `${chip.name}: used in GW${chip.used_gw}`;

                    return `<span class="chip ${cls}" title="${tip}">`
                         + `${chip.short}</span>`;

                }).join("");

            let curryTag = "";

            if (player.curries > 3) {

                curryTag = ` 🍛 x${player.curries}`;

            }
            else if (player.curries > 0) {

                curryTag = " " + "🍛".repeat(player.curries);
            }

            let hitTag = "";

            if (player.hit_cost > 0) {

                hitTag =
                    `<span class="movement down"`
                    + ` title="Transfer hit taken this gameweek">`
                    + `🤡 -${player.hit_cost}</span>`;
            }

            let moveTag = "";

            if (player.last_move === "promoted") {

                moveTag =
                    `<span class="movement up">▲ PROMOTED</span>`;

            }
            else if (player.last_move === "relegated") {

                moveTag =
                    `<span class="movement down">▼ RELEGATED</span>`;
            }

            let medal = "";

            if (
                player.rank === 1
            ) {
                medal = "🥇";
            }
            else if (
                player.rank === 2
            ) {
                medal = "🥈";
            }
            else if (
                player.rank === 3
            ) {
                medal = "🥉";
            }
            else {
                medal =
                    player.rank;
            }


            let movement = "";


            if (
                player.movement > 0
            ) {

                movement =
                    `<span class="movement up">
                        ↑ ${player.movement}
                     </span>`;

            }
            else if (
                player.movement < 0
            ) {

                movement =
                    `<span class="movement down">
                        ↓ ${Math.abs(player.movement)}
                     </span>`;

            }
            else {

                movement =
                    `<span class="movement same">
                        —
                     </span>`;
            }


            const row =
                document.createElement(
                    "div"
                );

            row.className =
                "player";


            let rankClass = "";

            if (
                player.rank === 1
            ) {
                rankClass =
                    "rank1";
            }
            else if (
                player.rank === 2
            ) {
                rankClass =
                    "rank2";
            }
            else if (
                player.rank === 3
            ) {
                rankClass =
                    "rank3";
            }


            const gwText = gwSigned(player.gw_points);

            const gwColour =
                player.gw_points < 0 ? "#ff4d6d" : "#00ff87";

            row.innerHTML = `

                <div
                    class="position ${rankClass}">
                    ${medal}
                </div>

                <div>

                    <div class="player-name">
                        ${escapeHtml(
                            player.manager
                        )}

                        ${curryTag}

                        ${hitTag}

                        ${movement}

                        ${moveTag}
                    </div>

                    <div class="team-name">
                        ${escapeHtml(
                            player.team
                        )}
                    </div>

                    <div class="chips">
                        ${chipsHtml}
                    </div>

                </div>

                <div class="total">

                    ${player.total}

                    <br>

                    <small style="font-size: 11px; color: #76958a;">
                        ${periodLabel} total
                    </small>

                </div>

                <div class="gw" style="color: ${gwColour};">

                    ${gwText}
                    <br>

                    <small>
                        ${gwLabel}
                    </small>

                </div>

            `;


            container.appendChild(
                row
            );

        }
    );
}


/* ==========================================================
   CAPTAINS THIS GAMEWEEK
   ========================================================== */

function renderCaptains(players) {

    const container =
        document.getElementById("captains");

    container.innerHTML = "";

    players.forEach(player => {

        const card =
            document.createElement("div");

        card.className = "captain-card";

        card.innerHTML = `
            <div class="captain-manager">
                ${player.rank}. ${escapeHtml(player.manager)}
            </div>

            <div class="captain-team">
                ${escapeHtml(player.team)}
            </div>

            <div class="captain-name">
                🧢 ${escapeHtml(player.captain)}
            </div>

            <div class="captain-points">
                ${player.captain_points} pts
                <small>(captain)</small>
            </div>

            <div class="captain-team">
                Team GW points: ${gwSigned(player.gw_points)}
            </div>
        `;

        container.appendChild(card);
    });
}


/* ==========================================================
   POINTS LEFT ON THE BENCH (season total)
   ========================================================== */

function renderBench(data) {

    const players = [...data.players].sort(
        (a, b) =>
            b.bench_total - a.bench_total
            || a.manager.localeCompare(b.manager)
    );

    const lastGw = data.bench_gw;

    document.getElementById("benchNote").innerText =
        lastGw > 0
            ? `Counted once each gameweek has finished (up to GW${lastGw}). `
              + `Bench Boost weeks aren't counted.`
            : "No finished gameweeks yet.";

    let rows = "";

    players.forEach((p, index) => {

        const weeks = (p.bench_by_gw || [])
            .filter(b => b.points > 0)
            .map(b => `GW${b.gw} (${b.points})`)
            .join(", ") || "Nothing wasted 🎉";

        rows += `
            <tr>
                <td>${index + 1}</td>
                <td>
                    <strong>${escapeHtml(p.manager)}</strong>
                    <div class="hits-weeks">${escapeHtml(p.team)}</div>
                    <div class="hits-weeks">${weeks}</div>
                </td>
                <td>${p.bench_last}</td>
                <td class="hits-points">${p.bench_total}</td>
            </tr>
        `;
    });

    document.getElementById("bench").innerHTML = `
        <table class="hits-table">

            <tr>
                <th>#</th>
                <th>MANAGER</th>
                <th>${lastGw > 0 ? "GW" + lastGw : "LAST GW"}</th>
                <th>SEASON TOTAL</th>
            </tr>

            ${rows}

        </table>
    `;
}


/* ==========================================================
   CHIP SCORES
   ========================================================== */

function renderChipScores(players) {

    const columns = [
        { key: "wildcard", label: "WILDCARD" },
        { key: "freehit", label: "FREE HIT" },
        { key: "bboost", label: "BENCH BOOST" },
        { key: "3xc", label: "TRIPLE CAPTAIN" }
    ];

    let rows = "";

    players.forEach(p => {

        let cells = "";

        columns.forEach(col => {

            const results = (p.chip_results || {})[col.key] || [];

            if (!results.length) {

                cells += `<td style="color: #5d776c;">—</td>`;

                return;
            }

            const lines = results.map(r => {

                const live = r.live ? " (live)" : "";

                if (col.key === "3xc") {

                    return `<div class="hits-weeks">GW${r.gw}${live}</div>`
                         + `<div><strong>${escapeHtml(r.player)}</strong> `
                         + `<span style="color: #00ff87; font-weight: 900;">`
                         + `${r.points}</span></div>`;
                }

                return `<div class="hits-weeks">GW${r.gw}${live}</div>`
                     + `<div style="color: #00ff87; font-weight: 900;">`
                     + `${r.points}</div>`;

            }).join("");

            cells += `<td>${lines}</td>`;
        });

        rows += `
            <tr>
                <td>
                    <strong>${escapeHtml(p.manager)}</strong>
                    <div class="hits-weeks">${escapeHtml(p.team)}</div>
                </td>
                ${cells}
            </tr>
        `;
    });

    document.getElementById("chipScores").innerHTML = `
        <table class="hits-table">

            <tr>
                <th>MANAGER</th>
                ${columns.map(c => `<th>${c.label}</th>`).join("")}
            </tr>

            ${rows}

        </table>
    `;
}


/* ==========================================================
   HITS TRACKER
   ========================================================== */

async function openHits() {

    document.getElementById("hitsModal").style.display = "flex";

    const box = document.getElementById("hitsResults");

    box.style.display = "block";

    box.innerHTML = `<div class="chip-note">Loading hits...</div>`;

    try {

        const response = await fetch("/api/hits");

        const data = await response.json();

        if (!response.ok) {

            throw new Error(data.error || "Unable to load the hits");
        }

        renderHits(data);

    } catch (err) {

        box.innerHTML =
            `<div class="hits-error">${escapeHtml(err.message)}</div>`;
    }
}

function closeHits() {

    document.getElementById("hitsModal").style.display = "none";

    // Wipe the data from the page when closed
    document.getElementById("hitsResults").innerHTML = "";

    lastHitsData = null;
}

function renderHits(data) {

    lastHitsData = data;

    let rows = "";

    data.managers.forEach((m, index) => {

        const weeks = m.hits.length
            ? m.hits.map(h => `GW${h.gw} (-${h.points})`).join(", ")
            : "No hits 🎉";

        rows += `
            <tr>
                <td>${index + 1}</td>
                <td>
                    <strong>${escapeHtml(m.manager)}</strong>
                    ${m.total_hits > 0 ? " 🤡" : ""}
                    <div class="hits-weeks">${escapeHtml(m.team)}</div>
                    <div class="hits-weeks">${weeks}</div>
                </td>
                <td>${m.total_hits}</td>
                <td class="hits-points">
                    ${m.total_points > 0 ? "-" + m.total_points : "0"}
                </td>
            </tr>
        `;
    });

    const note = data.from_saved
        ? "<br><small>FPL could not be reached - showing saved data.</small>"
        : "";

    document.getElementById("hitsResults").innerHTML = `

        <button onclick="downloadHitsPng()"
                style="margin-bottom: 15px;">
            📸 SAVE AS PNG
        </button>

        <div class="hits-summary">

            Points dropped on hits this season:
            <strong>-${data.total_points}</strong>
            (${data.total_hits} extra transfers)

            <br>

            <small>Updated ${escapeHtml(data.updated || "never")}</small>

            ${note}

        </div>

        <table class="hits-table">

            <tr>
                <th>#</th>
                <th>MANAGER</th>
                <th>HITS</th>
                <th>POINTS LOST</th>
            </tr>

            ${rows}

        </table>
    `;
}


/* ==========================================================
   HITS TRACKER - SAVE AS PNG
   ========================================================== */

function wrapText(ctx, text, maxWidth) {

    const words = text.split(" ");
    const lines = [];
    let line = "";

    words.forEach(word => {

        const test = line ? line + " " + word : word;

        if (ctx.measureText(test).width > maxWidth && line) {
            lines.push(line);
            line = word;
        } else {
            line = test;
        }
    });

    if (line) {
        lines.push(line);
    }

    return lines;
}

function downloadHitsPng() {

    if (!lastHitsData) {
        return;
    }

    const data = lastHitsData;

    const green = "#00FF87";
    const dark = "#061A14";
    const rowBg = "#0D3024";
    const white = "#FFFFFF";
    const grey = "#9AB3A8";
    const red = "#FF4D6D";
    const font = "Arial, Helvetica, sans-serif";

    const W = 1000;
    const S = 2;                     // 2x for a sharp image on phones
    const pad = 40;

    const nameX = pad + 55;
    const hitsRight = W - pad - 170;
    const ptsRight = W - pad;
    const textW = hitsRight - 80 - nameX;

    // Work out how tall each row needs to be
    const measure = document.createElement("canvas").getContext("2d");
    measure.font = `14px ${font}`;

    const rows = data.managers.map(m => {

        const weeks = m.hits.length
            ? m.hits
                .map(h => `GW${h.gw}\u00A0(-${h.points})`)
                .join(", ")
            : "No hits 🎉";

        const lines = wrapText(measure, weeks, textW);

        return {
            m: m,
            lines: lines,
            h: 68 + lines.length * 18 + 12
        };
    });

    const headerH = 120;
    const summaryH = 100;
    const tableHeadH = 40;
    const footerH = 70;

    const rowsH = rows.reduce((sum, r) => sum + r.h + 8, 0);

    const H = headerH + summaryH + tableHeadH + rowsH + footerH;

    const canvas = document.createElement("canvas");

    canvas.width = W * S;
    canvas.height = H * S;

    const ctx = canvas.getContext("2d");

    ctx.scale(S, S);

    // Background
    ctx.fillStyle = dark;
    ctx.fillRect(0, 0, W, H);

    // Header
    ctx.fillStyle = green;
    ctx.fillRect(0, 0, W, headerH);

    ctx.fillStyle = dark;
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";

    ctx.font = `bold 44px ${font}`;
    ctx.fillText("🤡 FPL HITS TRACKER", pad, 62);

    ctx.font = `16px ${font}`;
    ctx.fillText(
        "Points dropped on transfer hits this season",
        pad + 4,
        94
    );

    // Summary box
    let y = headerH + 25;

    ctx.fillStyle = rowBg;
    ctx.beginPath();
    ctx.roundRect(pad, y, W - pad * 2, 65, 12);
    ctx.fill();

    ctx.fillStyle = red;
    ctx.font = `bold 30px ${font}`;
    ctx.fillText(`-${data.total_points}`, pad + 20, y + 42);

    const totalW = ctx.measureText(`-${data.total_points}`).width;

    ctx.fillStyle = grey;
    ctx.font = `16px ${font}`;
    ctx.fillText(
        `points lost  •  ${data.total_hits} extra transfers`,
        pad + 20 + totalW + 15,
        y + 40
    );

    y += 65 + 20;

    // Table heading
    ctx.fillStyle = grey;
    ctx.font = `bold 13px ${font}`;

    ctx.textAlign = "left";
    ctx.fillText("#", pad + 10, y + 15);
    ctx.fillText("MANAGER", nameX, y + 15);

    ctx.textAlign = "right";
    ctx.fillText("HITS", hitsRight, y + 15);
    ctx.fillText("POINTS LOST", ptsRight - 15, y + 15);

    y += tableHeadH - 10;

    // Rows
    rows.forEach((r, index) => {

        const m = r.m;

        ctx.fillStyle = rowBg;
        ctx.beginPath();
        ctx.roundRect(pad, y, W - pad * 2, r.h, 12);
        ctx.fill();

        // Position
        ctx.fillStyle = white;
        ctx.font = `bold 22px ${font}`;
        ctx.textAlign = "left";
        ctx.fillText(String(index + 1), pad + 15, y + 34);

        // Manager (+ clown if they've taken a hit)
        ctx.font = `bold 20px ${font}`;
        ctx.fillText(m.manager, nameX, y + 30);

        if (m.total_hits > 0) {

            const nameW = ctx.measureText(m.manager).width;

            ctx.font = `20px ${font}`;
            ctx.fillText("🤡", nameX + nameW + 8, y + 30);
        }

        // Team
        ctx.fillStyle = grey;
        ctx.font = `14px ${font}`;
        ctx.fillText(m.team, nameX, y + 50);

        // Gameweeks of hits
        ctx.fillStyle = m.hits.length ? "#A9C5B9" : green;
        ctx.font = `14px ${font}`;

        r.lines.forEach((line, i) => {
            ctx.fillText(line, nameX, y + 72 + i * 18);
        });

        // Number of hits
        ctx.textAlign = "right";
        ctx.fillStyle = white;
        ctx.font = `bold 22px ${font}`;
        ctx.fillText(String(m.total_hits), hitsRight, y + 34);

        // Points lost
        ctx.fillStyle = m.total_points > 0 ? red : grey;
        ctx.font = `bold 26px ${font}`;
        ctx.fillText(
            m.total_points > 0 ? `-${m.total_points}` : "0",
            ptsRight - 15,
            y + 36
        );

        y += r.h + 8;
    });

    // Footer
    ctx.textAlign = "left";
    ctx.fillStyle = grey;
    ctx.font = `14px ${font}`;
    ctx.fillText(
        `Updated: ${data.updated || "never"}`,
        pad,
        H - 38
    );

    ctx.fillStyle = green;
    ctx.fillText(
        "Fantasy Premier League Mini League",
        pad,
        H - 16
    );

    // Download
    canvas.toBlob(blob => {

        const url = URL.createObjectURL(blob);

        const link = document.createElement("a");

        link.href = url;
        link.download = "FPL_Hits_Tracker.png";

        document.body.appendChild(link);
        link.click();
        link.remove();

        URL.revokeObjectURL(url);

    }, "image/png");
}


/* ==========================================================
   CAPTAINS - SAVE AS PNG
   ========================================================== */

function fitText(ctx, text, maxW) {

    if (ctx.measureText(text).width <= maxW) {
        return text;
    }

    while (text.length > 1 && ctx.measureText(text + "…").width > maxW) {
        text = text.slice(0, -1);
    }

    return text + "…";
}

function downloadCaptainsPng() {

    if (!currentData) {

        alert("Please update the FPL data first.");

        return;
    }

    const data = currentData;

    const green = "#00FF87";
    const dark = "#061A14";
    const cardBg = "#0A241B";
    const rowBg = "#0D3024";
    const white = "#FFFFFF";
    const grey = "#9AB3A8";
    const font = "Arial, Helvetica, sans-serif";

    const W = 1400;
    const S = 2;                     // 2x for a sharp image on phones
    const pad = 40;
    const gap = 25;
    const colW = (W - pad * 2 - gap) / 2;

    const cardH = 118;
    const cardGap = 12;
    const headerH = 130;
    const colTitleH = 50;
    const footerH = 80;

    const left = data.players.filter(p => p.league === "championship");
    const right = data.players.filter(p => p.league === "challenger");

    const rowsN = Math.max(left.length, right.length);

    const H = headerH + colTitleH + rowsN * (cardH + cardGap) + footerH;

    const canvas = document.createElement("canvas");

    canvas.width = W * S;
    canvas.height = H * S;

    const ctx = canvas.getContext("2d");

    ctx.scale(S, S);

    ctx.textBaseline = "alphabetic";

    // Background
    ctx.fillStyle = dark;
    ctx.fillRect(0, 0, W, H);

    // Header
    ctx.fillStyle = green;
    ctx.fillRect(0, 0, W, headerH);

    ctx.fillStyle = dark;
    ctx.textAlign = "left";

    ctx.font = `bold 48px ${font}`;
    ctx.fillText("🧢 FPL CAPTAINS", pad, 66);

    ctx.font = `18px ${font}`;
    ctx.fillText(
        `${data.league_name}  •  GAMEWEEK ${data.gameweek}`
        + (data.live ? "  •  LIVE" : ""),
        pad + 4,
        102
    );

    function drawColumn(x, title, list) {

        // Column title
        ctx.textAlign = "left";
        ctx.fillStyle = grey;
        ctx.font = `bold 16px ${font}`;
        ctx.fillText(title, x + 5, headerH + 32);

        list.forEach((p, i) => {

            const y = headerH + colTitleH + i * (cardH + cardGap);

            ctx.fillStyle = rowBg;
            ctx.beginPath();
            ctx.roundRect(x, y, colW, cardH, 14);
            ctx.fill();

            const textW = colW - 200;

            // Manager
            ctx.textAlign = "left";
            ctx.fillStyle = white;
            ctx.font = `bold 22px ${font}`;
            ctx.fillText(
                fitText(ctx, `${p.rank}. ${p.manager}`, textW),
                x + 22,
                y + 36
            );

            // Team
            ctx.fillStyle = grey;
            ctx.font = `15px ${font}`;
            ctx.fillText(fitText(ctx, p.team, textW), x + 22, y + 60);

            // Captain
            ctx.fillStyle = green;
            ctx.font = `bold 20px ${font}`;
            ctx.fillText(
                fitText(ctx, `🧢 ${p.captain}`, textW),
                x + 22,
                y + 90
            );

            // Captain points
            ctx.textAlign = "right";
            ctx.fillStyle = white;
            ctx.font = `bold 38px ${font}`;
            ctx.fillText(`${p.captain_points}`, x + colW - 22, y + 56);

            ctx.fillStyle = grey;
            ctx.font = `14px ${font}`;
            ctx.fillText("captain pts", x + colW - 22, y + 78);

            ctx.fillStyle = green;
            ctx.fillText(`Team GW: ${gwSigned(p.gw_points)}`, x + colW - 22, y + 100);
        });
    }

    drawColumn(pad, "CHAMPIONSHIP LEAGUE", left);
    drawColumn(pad + colW + gap, "CHALLENGER LEAGUE", right);

    // Footer
    ctx.textAlign = "left";
    ctx.fillStyle = grey;
    ctx.font = `14px ${font}`;
    ctx.fillText(`Updated: ${data.updated}`, pad, H - 42);

    ctx.fillStyle = green;
    ctx.fillText("Fantasy Premier League Mini League", pad, H - 18);

    // Download
    canvas.toBlob(blob => {

        const url = URL.createObjectURL(blob);

        const link = document.createElement("a");

        link.href = url;
        link.download = `FPL_Captains_GW${data.gameweek}.png`;

        document.body.appendChild(link);
        link.click();
        link.remove();

        URL.revokeObjectURL(url);

    }, "image/png");
}


/* ==========================================================
   SECURITY
   ========================================================== */

/* ==========================================================
   FROZEN GW1 TEAM (no transfers)
   ========================================================== */

let lastGw1Data = null;

async function loadGw1Team() {

    const box = document.getElementById("gw1Team");

    if (!box.innerHTML.trim()) {
        box.innerHTML =
            `<div class="chip-note">Working out GW1 teams...</div>`;
    }

    try {

        const response = await fetch("/api/gw1-team");
        const data = await response.json();

        if (data.error) {
            throw new Error(data.error);
        }

        lastGw1Data = data;

        renderGw1Team(data);

    } catch (error) {

        console.error(error);

        box.innerHTML =
            `<div class="chip-note">Couldn't load this table: `
            + `${escapeHtml(error.message)}</div>`;
    }
}

function renderGw1Team(data) {

    const box = document.getElementById("gw1Team");

    if (!data.finished_gw) {
        box.innerHTML =
            `<div class="chip-note">No finished gameweeks yet.</div>`;
        return;
    }

    let headers = "";

    for (let gw = 1; gw <= data.finished_gw; gw++) {
        headers += `<th>GW${gw}</th>`;
    }

    let rows = "";

    data.managers.forEach((m, index) => {

        if (!m.has_team) {

            rows += `
                <tr>
                    <td>${index + 1}</td>
                    <td>
                        <strong>${escapeHtml(m.manager)}</strong>
                        <div class="hits-weeks">${escapeHtml(m.team)}</div>
                    </td>
                    <td colspan="${data.finished_gw + 3}"
                        style="color: #5d776c;">
                        No GW1 team found
                    </td>
                </tr>
            `;

            return;
        }

        const diff = (m.actual === null || m.actual === undefined)
            ? null
            : m.actual - m.total;

        let diffCell = `<td style="color: #5d776c;">—</td>`;

        if (diff !== null) {

            const colour = diff > 0 ? "#00ff87"
                         : diff < 0 ? "#ff4d6d"
                         : "#a9c5b9";

            const sign = diff > 0 ? "+" : "";

            diffCell = `<td style="color: ${colour}; font-weight: 900;">`
                     + `${sign}${diff}</td>`;
        }

        let cells = "";

        for (let i = 0; i < data.finished_gw; i++) {

            cells += (m.running[i] === undefined)
                ? `<td style="color: #5d776c;">—</td>`
                : `<td>${m.running[i]}</td>`;
        }

        rows += `
            <tr>
                <td>${index + 1}</td>
                <td style="white-space: nowrap;">
                    <strong>${escapeHtml(m.manager)}</strong>
                    <div class="hits-weeks">${escapeHtml(m.team)}</div>
                </td>
                <td style="color: #00ff87; font-weight: 900;">${m.total}</td>
                <td>${m.actual === null ? "—" : m.actual}</td>
                ${diffCell}
                ${cells}
            </tr>
        `;
    });

    box.innerHTML = `
        <table class="hits-table">

            <tr>
                <th>#</th>
                <th>MANAGER</th>
                <th>GW1 TEAM</th>
                <th>ACTUAL</th>
                <th>TRANSFERS EFFECT</th>
                ${headers}
            </tr>

            ${rows}

        </table>
    `;
}


/* ==========================================================
   FROZEN GW1 TEAM - SAVE AS PNG
   ========================================================== */

function downloadGw1Png() {

    if (!lastGw1Data || !lastGw1Data.finished_gw) {

        alert("The frozen team table hasn't loaded yet.");

        return;
    }

    const data = lastGw1Data;
    const n = data.finished_gw;

    const green = "#00FF87";
    const dark = "#061A14";
    const rowBg = "#0D3024";
    const white = "#FFFFFF";
    const grey = "#9AB3A8";
    const red = "#FF4D6D";
    const font = "Arial, Helvetica, sans-serif";

    const S = 2;
    const pad = 40;

    const rankW = 40;
    const nameW = 250;
    const statW = 110;
    const gwW = 56;

    const tableW = rankW + nameW + statW * 3 + n * gwW;

    const W = Math.max(tableW + pad * 2, 1300);

    const rowH = 50;
    const gap = 4;
    const headerH = 120;
    const colHeadH = 40;
    const legendH = 90;
    const footerH = 70;

    const H = headerH + 25 + colHeadH
            + data.managers.length * (rowH + gap)
            + legendH + footerH;

    const canvas = document.createElement("canvas");

    canvas.width = W * S;
    canvas.height = H * S;

    const ctx = canvas.getContext("2d");

    ctx.scale(S, S);
    ctx.textBaseline = "alphabetic";

    // Background + header
    ctx.fillStyle = dark;
    ctx.fillRect(0, 0, W, H);

    ctx.fillStyle = green;
    ctx.fillRect(0, 0, W, headerH);

    ctx.fillStyle = dark;
    ctx.textAlign = "left";
    ctx.font = `bold 46px ${font}`;
    ctx.fillText("🧊 FROZEN GW1 TEAM", pad, 64);

    ctx.font = `18px ${font}`;
    ctx.fillText(
        `No transfers, no hits, no chips  •  GW1-${n}`,
        pad + 4,
        100
    );

    // Column headings
    let y = headerH + 25;

    const x0 = pad;
    const nameX = x0 + rankW;
    const statX = nameX + nameW;
    const firstGwX = statX + statW * 3;

    ctx.fillStyle = grey;
    ctx.font = `bold 13px ${font}`;

    ctx.textAlign = "center";
    ctx.fillText("#", x0 + rankW / 2, y + 26);

    ctx.textAlign = "left";
    ctx.fillText("MANAGER", nameX, y + 26);

    ctx.textAlign = "center";
    ctx.fillText("GW1 TEAM", statX + statW / 2, y + 26);
    ctx.fillText("ACTUAL", statX + statW * 1.5, y + 26);
    ctx.fillText("TRANSFERS", statX + statW * 2.5, y + 20);
    ctx.fillText("EFFECT", statX + statW * 2.5, y + 35);

    for (let i = 0; i < n; i++) {
        ctx.fillText(`GW${i + 1}`, firstGwX + i * gwW + gwW / 2, y + 26);
    }

    y += colHeadH;

    // Rows
    data.managers.forEach((m, index) => {

        ctx.fillStyle = rowBg;
        ctx.beginPath();
        ctx.roundRect(x0, y, tableW, rowH, 10);
        ctx.fill();

        // Rank
        ctx.textAlign = "center";
        ctx.fillStyle = white;
        ctx.font = `bold 18px ${font}`;
        ctx.fillText(String(index + 1), x0 + rankW / 2, y + 31);

        // Manager + team
        ctx.textAlign = "left";
        ctx.fillStyle = white;
        ctx.font = `bold 18px ${font}`;
        ctx.fillText(fitText(ctx, m.manager, nameW - 14), nameX, y + 23);

        ctx.fillStyle = grey;
        ctx.font = `12px ${font}`;
        ctx.fillText(fitText(ctx, m.team, nameW - 14), nameX, y + 40);

        if (!m.has_team) {

            ctx.fillStyle = "#5d776c";
            ctx.font = `15px ${font}`;
            ctx.fillText("No GW1 team found", statX, y + 31);

            y += rowH + gap;

            return;
        }

        // Frozen total, actual total, effect
        ctx.textAlign = "center";

        ctx.fillStyle = green;
        ctx.font = `bold 20px ${font}`;
        ctx.fillText(String(m.total), statX + statW / 2, y + 32);

        ctx.fillStyle = white;
        ctx.fillText(
            (m.actual === null || m.actual === undefined)
                ? "—" : String(m.actual),
            statX + statW * 1.5,
            y + 32
        );

        if (m.actual === null || m.actual === undefined) {

            ctx.fillStyle = "#5d776c";
            ctx.fillText("—", statX + statW * 2.5, y + 32);

        } else {

            const diff = m.actual - m.total;

            ctx.fillStyle = diff > 0 ? green : diff < 0 ? red : grey;
            ctx.fillText(
                (diff > 0 ? "+" : "") + diff,
                statX + statW * 2.5,
                y + 32
            );
        }

        // Running total after each gameweek
        ctx.font = `15px ${font}`;

        for (let i = 0; i < n; i++) {

            const v = m.running[i];

            ctx.fillStyle = v === undefined ? "#5d776c" : white;
            ctx.fillText(
                v === undefined ? "—" : String(v),
                firstGwX + i * gwW + gwW / 2,
                y + 31
            );
        }

        y += rowH + gap;
    });

    // Legend + footer
    y += 15;

    ctx.textAlign = "left";
    ctx.fillStyle = grey;
    ctx.font = `14px ${font}`;

    ctx.fillText(
        "GW1 TEAM = running total if they had kept their GW1 squad all season "
        + "(same XI, bench order and captain; auto-subs still apply)",
        pad,
        y + 20
    );

    ctx.fillText(
        "TRANSFERS EFFECT = actual total minus frozen total, "
        + "so green means their changes paid off",
        pad,
        y + 44
    );

    ctx.fillText(
        `Counted once each gameweek has finished (up to GW${n})`,
        pad,
        y + 68
    );

    ctx.fillText(
        `Updated: ${new Date().toLocaleString()}`,
        pad,
        H - 38
    );

    ctx.fillStyle = green;
    ctx.fillText("Fantasy Premier League Mini League", pad, H - 16);

    // Download
    canvas.toBlob(blob => {

        const url = URL.createObjectURL(blob);

        const link = document.createElement("a");

        link.href = url;
        link.download = `FPL_Frozen_GW1_Team_GW${n}.png`;

        document.body.appendChild(link);
        link.click();
        link.remove();

        URL.revokeObjectURL(url);

    }, "image/png");
}


/* ==========================================================
   CURRY TABLE
   ========================================================== */

let lastCurryData = null;

async function loadCurryTable() {

    const box = document.getElementById("curryTable");

    if (!box.innerHTML.trim()) {
        box.innerHTML =
            `<div class="chip-note">Working out the curry table...</div>`;
    }

    try {

        const response = await fetch("/api/curry-table");
        const data = await response.json();

        if (data.error) {
            throw new Error(data.error);
        }

        lastCurryData = data;

        renderCurryTable(data);

    } catch (error) {

        console.error(error);

        box.innerHTML =
            `<div class="chip-note">Couldn't load this table: `
            + `${escapeHtml(error.message)}</div>`;
    }
}

function curryRule(data) {

    return data.owed_by === "all"
        ? "everyone else in the same league owes that league's winner a curry"
        : "last place in a league owes the winner of the SAME league a curry";
}

// "🏆 Dave (31) owes Sam" - the emoji shows which league it is
function curryOwesText(l) {

    if (!l.owes.length) {
        return "—";
    }

    const names = l.owes
        .map(o => `${o.manager} (${o.points})`)
        .join(", ");

    return `${l.emoji} ${names} `
         + `${l.owes.length === 1 ? "owes" : "owe"} ${l.winner.manager}`;
}

// The columns, in order: GW1, GW2 ... with a period total column after
// the last gameweek of each period (live period goes on the end)
function curryColumns(data) {

    const ends = {};
    let live = null;

    data.periods.forEach(p => {
        if (p.complete) {
            ends[p.last] = p;
        } else {
            live = p;
        }
    });

    const cols = [];

    for (let gw = 1; gw <= data.shown_gw; gw++) {

        cols.push({ type: "gw", gw: gw });

        if (ends[gw]) {
            cols.push({ type: "period", period: ends[gw] });
        }
    }

    if (live) {
        cols.push({ type: "period", period: live });
    }

    return cols;
}

function renderCurryTable(data) {

    document.getElementById("curryNote").innerText =
        `Every gameweek from GW1. After each period there is a period `
        + `column: gold 🍛 = won the curry, red OWES = `
        + curryRule(data) + `. Emoji = league: 🏆 Championship, `
        + `⚔️ Challenger, ⚽ single league (GW1-${data.single_until}, `
        + `before the split). B = backdated period (played before `
        + `GW${data.start_gw}).`
        + (data.live ? ` GW${data.shown_gw} is live.` : "");

    const cols = curryColumns(data);

    let head = "";

    cols.forEach(c => {

        if (c.type === "gw") {
            head += `<th>GW${c.gw}</th>`;
        } else {
            const p = c.period;
            head += `<th class="curry-period">${p.label}`
                  + (p.complete ? "" : "*")
                  + `<div class="curry-sub">GW${p.first}-${p.last}</div></th>`;
        }
    });

    let rows = "";
    let lastLeague = null;

    data.managers.forEach(m => {

        if (m.league !== lastLeague) {

            lastLeague = m.league;

            rows += `<tr><td colspan="${cols.length + 4}" `
                  + `style="text-align:left; color:#00ff87; font-weight:900;">`
                  + (m.league === "championship"
                        ? "🏆 CHAMPIONSHIP" : "⚔️ CHALLENGER")
                  + `</td></tr>`;
        }

        let cells = "";

        cols.forEach(c => {

            if (c.type === "gw") {

                const v = m.gws[String(c.gw)];

                cells += (v === undefined || v === null)
                    ? `<td style="color: #5d776c;">—</td>`
                    : `<td${v < 0 ? ' style="color:#ff4d6d;"' : ""}>${v}</td>`;

                return;
            }

            const r = c.period.by_entry[String(m.entry)];

            if (!r) {
                cells += `<td class="curry-period" style="color:#5d776c;">—</td>`;
                return;
            }

            if (r.status === "won") {
                cells += `<td class="curry-period curry-won">${r.points}`
                       + `<div class="curry-sub">🍛 WON ${r.emoji}</div></td>`;
            } else if (r.status === "owes") {
                cells += `<td class="curry-period curry-owes">${r.points}`
                       + `<div class="curry-sub">OWES ${r.emoji}</div></td>`;
            } else {
                cells += `<td class="curry-period">${r.points}</td>`;
            }
        });

        rows += `
            <tr>
                <td>${m.rank}</td>
                <td>
                    <strong>${escapeHtml(m.manager)}</strong>
                    <div class="hits-weeks">${escapeHtml(m.team)}</div>
                </td>
                <td style="color:#ffd700; font-weight:900;">
                    ${m.curries_won ? "🍛 " + m.curries_won : "0"}
                </td>
                <td style="color:${m.curries_owed ? "#ff4d6d" : "#5d776c"}; font-weight:900;">
                    ${m.curries_owed}
                </td>
                ${cells}
            </tr>
        `;
    });

    document.getElementById("curryTable").innerHTML = `
        <table class="hits-table curry-table">
            <tr>
                <th>#</th>
                <th>MANAGER</th>
                <th>WON</th>
                <th>OWES</th>
                ${head}
            </tr>
            ${rows}
        </table>
    `;

    // ---- results by period ----
    let results = "";

    data.periods.forEach(p => {

        p.leagues.forEach(l => {

            const owes = p.complete
                ? escapeHtml(curryOwesText(l))
                : "—";

            results += `
                <tr>
                    <td>${p.label}${p.complete ? "" : "*"}
                        <div class="hits-weeks">GW${p.first}-${p.last}</div></td>
                    <td>${l.emoji} ${l.label}</td>
                    <td style="color:#ffd700; font-weight:900;">
                        ${p.complete ? "🍛 " : "leading: "}
                        ${escapeHtml(l.winner.manager)} (${l.winner.points})
                    </td>
                    <td style="color:#ff4d6d;">${owes}</td>
                </tr>
            `;
        });
    });

    document.getElementById("curryResults").innerHTML = results
        ? `<table class="hits-table">
               <tr><th>PERIOD</th><th>LEAGUE</th>
                   <th>WON THE CURRY</th><th>OWES A CURRY</th></tr>
               ${results}
           </table>
           <div class="chip-note" style="margin-top:8px;">* = period still in progress</div>`
        : `<div class="chip-note">No periods have started yet.</div>`;
}


function downloadCurryPng() {

    if (!lastCurryData) {

        alert("The curry table hasn't loaded yet.");

        return;
    }

    const data = lastCurryData;
    const cols = curryColumns(data);

    const green = "#00FF87";
    const dark = "#061A14";
    const rowBg = "#0D3024";
    const periodBg = "#14402F";
    const white = "#FFFFFF";
    const grey = "#9AB3A8";
    const red = "#FF4D6D";
    const gold = "#FFD700";
    const font = "Arial, Helvetica, sans-serif";

    const S = 2;
    const pad = 40;

    const rankW = 40;
    const nameW = 250;
    const tallyW = 70;
    const gwW = 48;
    const perW = 78;

    const colW = c => c.type === "gw" ? gwW : perW;

    const tableW =
        rankW + nameW + tallyW * 2
        + cols.reduce((sum, c) => sum + colW(c), 0);

    const W = Math.max(tableW + pad * 2, 1300);

    const rowH = 50;
    const gap = 4;
    const leagueH = 30;
    const headerH = 120;
    const colHeadH = 46;

    // Layout of the grid: league headings and manager rows
    const items = [];
    let lastLeague = null;

    data.managers.forEach(m => {

        if (m.league !== lastLeague) {
            lastLeague = m.league;
            items.push({ head: m.league });
        }

        items.push({ m: m });
    });

    const gridH = items.reduce(
        (sum, it) => sum + (it.head ? leagueH : rowH + gap), 0
    );

    const resultRows = [];

    data.periods.forEach(p => {
        p.leagues.forEach(l => resultRows.push({ p: p, l: l }));
    });

    const resultRowH = 34;
    const resultsH = resultRows.length
        ? 60 + resultRows.length * resultRowH
        : 0;

    const legendH = 105;
    const footerH = 70;

    const H = headerH + 25 + colHeadH + gridH + 25
            + resultsH + legendH + footerH;

    const canvas = document.createElement("canvas");

    canvas.width = W * S;
    canvas.height = H * S;

    const ctx = canvas.getContext("2d");

    ctx.scale(S, S);
    ctx.textBaseline = "alphabetic";

    // Background + header
    ctx.fillStyle = dark;
    ctx.fillRect(0, 0, W, H);

    ctx.fillStyle = green;
    ctx.fillRect(0, 0, W, headerH);

    ctx.fillStyle = dark;
    ctx.textAlign = "left";
    ctx.font = `bold 48px ${font}`;
    ctx.fillText("🍛 FPL CURRY TABLE", pad, 66);

    ctx.font = `18px ${font}`;
    ctx.fillText(
        `${data.league_name}  •  GW1-${data.shown_gw}`
        + (data.live ? "  (GW" + data.shown_gw + " live)" : ""),
        pad + 4,
        100
    );

    // Column headings
    let y = headerH + 25;

    const x0 = pad;
    const nameX = x0 + rankW;
    const tallyX = nameX + nameW;
    const firstColX = tallyX + tallyW * 2;

    ctx.fillStyle = grey;
    ctx.font = `bold 13px ${font}`;
    ctx.textAlign = "center";
    ctx.fillText("#", x0 + rankW / 2, y + 28);

    ctx.textAlign = "left";
    ctx.fillText("MANAGER", nameX, y + 28);

    ctx.textAlign = "center";
    ctx.fillText("WON", tallyX + tallyW / 2, y + 28);
    ctx.fillText("OWES", tallyX + tallyW * 1.5, y + 28);

    let cx = firstColX;

    cols.forEach(c => {

        const w = colW(c);

        if (c.type === "gw") {

            ctx.fillStyle = grey;
            ctx.font = `bold 13px ${font}`;
            ctx.fillText(`GW${c.gw}`, cx + w / 2, y + 28);

        } else {

            const p = c.period;

            ctx.fillStyle = green;
            ctx.font = `bold 14px ${font}`;
            ctx.fillText(
                `${p.label}${p.complete ? "" : "*"}`,
                cx + w / 2,
                y + 22
            );

            ctx.font = `bold 11px ${font}`;
            ctx.fillText(`GW${p.first}-${p.last}`, cx + w / 2, y + 38);
        }

        cx += w;
    });

    y += colHeadH;

    // Rows
    items.forEach(it => {

        if (it.head) {

            ctx.textAlign = "left";
            ctx.fillStyle = green;
            ctx.font = `bold 15px ${font}`;
            ctx.fillText(
                it.head === "championship"
                    ? "🏆 CHAMPIONSHIP" : "⚔️ CHALLENGER",
                x0 + 4,
                y + 21
            );

            y += leagueH;

            return;
        }

        const m = it.m;

        ctx.fillStyle = rowBg;
        ctx.beginPath();
        ctx.roundRect(x0, y, tableW, rowH, 10);
        ctx.fill();

        // Rank
        ctx.textAlign = "center";
        ctx.fillStyle = white;
        ctx.font = `bold 18px ${font}`;
        ctx.fillText(String(m.rank), x0 + rankW / 2, y + 31);

        // Manager + team
        ctx.textAlign = "left";
        ctx.fillStyle = white;
        ctx.font = `bold 18px ${font}`;
        ctx.fillText(fitText(ctx, m.manager, nameW - 14), nameX, y + 23);

        ctx.fillStyle = grey;
        ctx.font = `12px ${font}`;
        ctx.fillText(fitText(ctx, m.team, nameW - 14), nameX, y + 40);

        // Curries won / owed
        ctx.textAlign = "center";

        ctx.fillStyle = m.curries_won ? gold : grey;
        ctx.font = `bold 20px ${font}`;
        ctx.fillText(
            m.curries_won ? `🍛 ${m.curries_won}` : "0",
            tallyX + tallyW / 2,
            y + 32
        );

        ctx.fillStyle = m.curries_owed ? red : grey;
        ctx.fillText(String(m.curries_owed), tallyX + tallyW * 1.5, y + 32);

        // Gameweek / period cells
        let x = firstColX;

        cols.forEach(c => {

            const w = colW(c);

            if (c.type === "gw") {

                const v = m.gws[String(c.gw)];

                ctx.fillStyle =
                    (v === undefined || v === null) ? "#5d776c"
                    : v < 0 ? red : white;

                ctx.font = `16px ${font}`;
                ctx.fillText(
                    (v === undefined || v === null) ? "—" : String(v),
                    x + w / 2,
                    y + 31
                );

            } else {

                const r = c.period.by_entry[String(m.entry)];

                let bg = periodBg;

                if (r && r.status === "won") {
                    bg = "#5A4A0A";
                } else if (r && r.status === "owes") {
                    bg = "#5A1B2A";
                }

                ctx.fillStyle = bg;
                ctx.beginPath();
                ctx.roundRect(x + 3, y + 4, w - 6, rowH - 8, 8);
                ctx.fill();

                if (!r) {

                    ctx.fillStyle = "#5d776c";
                    ctx.font = `16px ${font}`;
                    ctx.fillText("—", x + w / 2, y + 31);

                } else if (r.status) {

                    const colour = r.status === "won" ? gold : red;

                    ctx.fillStyle = colour;
                    ctx.font = `bold 19px ${font}`;
                    ctx.fillText(String(r.points), x + w / 2, y + 26);

                    ctx.font = `bold 11px ${font}`;
                    ctx.fillText(
                        r.status === "won"
                            ? `🍛 WON ${r.emoji}`
                            : `OWES ${r.emoji}`,
                        x + w / 2,
                        y + 40
                    );

                } else {

                    ctx.fillStyle = white;
                    ctx.font = `bold 19px ${font}`;
                    ctx.fillText(String(r.points), x + w / 2, y + 31);
                }
            }

            x += w;
        });

        y += rowH + gap;
    });

    y += 25;

    // Results by period
    if (resultRows.length) {

        ctx.textAlign = "left";
        ctx.fillStyle = white;
        ctx.font = `bold 24px ${font}`;
        ctx.fillText("CURRY RESULTS BY PERIOD", pad, y + 20);

        y += 40;

        const xPeriod = pad + 10;
        const xLeague = pad + 190;
        const xWinner = pad + 340;
        const xOwes = pad + 700;

        ctx.fillStyle = grey;
        ctx.font = `bold 13px ${font}`;
        ctx.fillText("PERIOD", xPeriod, y + 14);
        ctx.fillText("LEAGUE", xLeague, y + 14);
        ctx.fillText("WON THE CURRY", xWinner, y + 14);
        ctx.fillText("OWES A CURRY", xOwes, y + 14);

        y += 20;

        resultRows.forEach(r => {

            const p = r.p;
            const l = r.l;

            ctx.fillStyle = rowBg;
            ctx.beginPath();
            ctx.roundRect(pad, y, W - pad * 2, resultRowH - 4, 8);
            ctx.fill();

            ctx.textAlign = "left";

            ctx.fillStyle = white;
            ctx.font = `bold 16px ${font}`;
            ctx.fillText(
                `${p.label}${p.complete ? "" : "*"}  (GW${p.first}-${p.last})`,
                xPeriod,
                y + 22
            );

            ctx.fillStyle = grey;
            ctx.font = `15px ${font}`;
            ctx.fillText(`${l.emoji} ${l.label}`, xLeague, y + 22);

            ctx.fillStyle = gold;
            ctx.font = `bold 16px ${font}`;
            ctx.fillText(
                fitText(
                    ctx,
                    (p.complete ? "🍛 " : "leading: ")
                        + `${l.winner.manager} (${l.winner.points})`,
                    xOwes - xWinner - 15
                ),
                xWinner,
                y + 22
            );

            ctx.fillStyle = red;
            ctx.font = `bold 15px ${font}`;

            const owesText = p.complete ? curryOwesText(l) : "—";

            ctx.fillText(
                fitText(ctx, owesText, W - pad - xOwes - 15),
                xOwes,
                y + 22
            );

            y += resultRowH;
        });

        y += 10;
    }

    // Legend + footer
    ctx.textAlign = "left";
    ctx.fillStyle = grey;
    ctx.font = `14px ${font}`;

    ctx.fillText(
        `🍛 WON = 1st in their league for the period  •  OWES = `
        + curryRule(data),
        pad,
        y + 20
    );

    ctx.fillText(
        `🏆 Championship  •  ⚔️ Challenger  •  ⚽ single league `
        + `(GW1-${data.single_until}, before the split)`,
        pad,
        y + 68
    );

    ctx.fillText(
        `B = backdated (before GW${data.start_gw})  •  `
        + `* = period still in progress  •  scores are after transfer hits`,
        pad,
        y + 44
    );

    ctx.fillText(`Updated: ${data.updated}`, pad, H - 38);

    ctx.fillStyle = green;
    ctx.fillText("Fantasy Premier League Mini League", pad, H - 16);

    // Download
    canvas.toBlob(blob => {

        const url = URL.createObjectURL(blob);

        const link = document.createElement("a");

        link.href = url;
        link.download = `FPL_Curry_Table_GW${data.shown_gw}.png`;

        document.body.appendChild(link);
        link.click();
        link.remove();

        URL.revokeObjectURL(url);

    }, "image/png");
}


function escapeHtml(
    text
) {

    const div =
        document.createElement(
            "div"
        );

    div.textContent =
        text;

    return div.innerHTML;
}


/* ==========================================================
   LIVE DEADLINE COUNTDOWN
   ========================================================== */

let deadlineTarget = null;

async function loadDeadline() {

    try {

        const response = await fetch("/api/deadline");
        const data = await response.json();

        if (data.error || !data.deadline) {
            deadlineTarget = null;
            document.getElementById("deadlineBox").style.display = "none";
            return;
        }

        deadlineTarget = new Date(data.deadline);

        document.getElementById("deadlineLabel").textContent =
            "GAMEWEEK " + data.gameweek + " DEADLINE";

        document.getElementById("deadlineWhen").textContent =
            deadlineTarget.toLocaleString([], {
                weekday: "short",
                day: "numeric",
                month: "short",
                hour: "2-digit",
                minute: "2-digit"
            });

        document.getElementById("deadlineBox").style.display =
            "inline-block";

        tickDeadline();

    } catch (error) {
        console.error(error);
    }
}

function tickDeadline() {

    if (!deadlineTarget) {
        return;
    }

    const box = document.getElementById("deadlineBox");
    const timeEl = document.getElementById("deadlineTime");

    const remaining = deadlineTarget.getTime() - Date.now();

    if (remaining <= 0) {

        timeEl.textContent = "DEADLINE PASSED";
        box.classList.remove("deadline-soon", "deadline-urgent");

        // Pick up the next gameweek's deadline
        deadlineTarget = null;
        setTimeout(loadDeadline, 30000);
        return;
    }

    const totalSeconds = Math.floor(remaining / 1000);

    const days = Math.floor(totalSeconds / 86400);
    const hours = Math.floor((totalSeconds % 86400) / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;

    const pad = (n) => String(n).padStart(2, "0");

    timeEl.textContent =
        (days > 0 ? days + "d " : "")
        + pad(hours) + "h "
        + pad(minutes) + "m "
        + pad(seconds) + "s";

    box.classList.toggle("deadline-urgent", remaining < 3600000);
    box.classList.toggle(
        "deadline-soon",
        remaining >= 3600000 && remaining < 86400000
    );
}

function downloadDeadline() {

    const label = document.getElementById("deadlineLabel").textContent;
    const time = document.getElementById("deadlineTime").textContent;
    const when = document.getElementById("deadlineWhen").textContent;
    const league = document.getElementById("leagueName").textContent.trim();
    const box = document.getElementById("deadlineBox");

    let timeColour = "#00ff87";

    if (box.classList.contains("deadline-urgent")) {
        timeColour = "#ff4d6d";
    } else if (box.classList.contains("deadline-soon")) {
        timeColour = "#ffd700";
    }

    const width = 900;
    const height = 360;
    const scale = 2;

    const canvas = document.createElement("canvas");
    canvas.width = width * scale;
    canvas.height = height * scale;

    const ctx = canvas.getContext("2d");
    ctx.scale(scale, scale);

    const font = "'Segoe UI', Arial, sans-serif";

    // Background
    ctx.fillStyle = "#061a14";
    ctx.fillRect(0, 0, width, height);

    // Green header strip
    const strip = ctx.createLinearGradient(0, 0, width, 0);
    strip.addColorStop(0, "#00ff87");
    strip.addColorStop(1, "#00c46a");
    ctx.fillStyle = strip;
    ctx.fillRect(0, 0, width, 70);

    ctx.textAlign = "center";
    ctx.textBaseline = "middle";

    ctx.fillStyle = "#061a14";
    ctx.font = "900 34px " + font;
    ctx.fillText("⚽ FPL LEAGUE", width / 2, 28);

    ctx.font = "600 16px " + font;
    ctx.fillText(league, width / 2, 54);

    // Label
    ctx.fillStyle = "#a9c5b9";
    ctx.font = "800 22px " + font;
    ctx.fillText(label, width / 2, 125);

    // Countdown (shrinks to fit)
    let size = 100;
    ctx.fillStyle = timeColour;

    do {
        ctx.font = "900 " + size + "px " + font;
        size -= 4;
    } while (ctx.measureText(time).width > width - 80 && size > 24);

    ctx.fillText(time, width / 2, 205);

    // Deadline date/time
    ctx.fillStyle = "#a9c5b9";
    ctx.font = "600 22px " + font;
    ctx.fillText(when, width / 2, 280);

    ctx.font = "500 14px " + font;
    ctx.fillText(
        "Snapshot taken " + new Date().toLocaleString(),
        width / 2,
        330
    );

    canvas.toBlob(function (blob) {

        const link = document.createElement("a");

        link.href = URL.createObjectURL(blob);

        link.download =
            label.toLowerCase().replace(/[^a-z0-9]+/g, "-")
            + ".png";

        document.body.appendChild(link);
        link.click();
        link.remove();

        setTimeout(function () {
            URL.revokeObjectURL(link.href);
        }, 1000);

    }, "image/png");
}

setInterval(tickDeadline, 1000);

// Deadlines can move, so check again every 10 minutes
setInterval(loadDeadline, 600000);


/* ==========================================================
   CHIP PLANNER MARQUEE
   ========================================================== */

async function loadChipPlanner() {

    const track = document.getElementById("chipMarqueeTrack");

    try {

        const response = await fetch("/api/chip-planner");
        const data = await response.json();

        track.textContent = "";

        let chars = 0;

        (data.items || []).forEach(function (text) {

            const span = document.createElement("span");

            span.className = "chip-marquee-item"
                + (/^(⚡|🚫)/.test(text) ? " alert" : "");

            span.textContent = text;

            track.appendChild(span);

            chars += text.length;
        });

        // steady reading speed however much text there is
        track.style.animationDuration =
            Math.max(30, Math.round(chars * 0.22)) + "s";

    } catch (error) {

        console.error(error);

        track.textContent = "Chip planner unavailable right now";
    }
}

loadChipPlanner();

// re-check every 30 minutes
setInterval(loadChipPlanner, 30 * 60 * 1000);


/* ==========================================================
   INITIAL LOAD
   ========================================================== */

refreshData();
loadDeadline();

// While a gameweek is being played, refresh the scores every 2 minutes
setInterval(function () {

    if (currentData && currentData.live) {
        refreshData();
    }

}, 120000);

</script>


</body>

</html>
"""


# ============================================================
# RUN SERVER
# ============================================================

if __name__ == "__main__":

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )
