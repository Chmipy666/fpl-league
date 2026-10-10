from flask import Flask, jsonify, render_template_string, send_file, request
import requests
import json
import os
import hmac
import time
from datetime import datetime
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

# ---- Hits tracker (password protected) ----
# Every transfer hit (-4, -8 ...) taken this season is saved here.
HITS_FILE = "fpl_hits.json"

# Password for the HITS TRACKER button.
# (Or set an environment variable called FPL_HITS_PASSWORD instead.)
HITS_PASSWORD = os.environ.get("FPL_HITS_PASSWORD", "Meg8tron123!!!")

# ---- Chips ----
# Each chip comes in two sets: one for GW1-19 and one for GW20-38.
# First-set chips expire after the GW19 deadline.
CHIP_SET_SPLIT_GW = 19

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


def get_current_gameweek():
    """Return the most recently finished Gameweek."""

    data = fpl_get("bootstrap-static/")

    finished_events = [
        event["id"]
        for event in data.get("events", [])
        if event.get("finished")
    ]

    if not finished_events:
        return 0

    return max(finished_events)


def get_live_gameweek():
    """Return the gameweek being played now (or the last finished one)."""

    data = fpl_get("bootstrap-static/")

    events = data.get("events", [])

    for event in events:
        if event.get("is_current"):
            return event["id"]

    finished = [e["id"] for e in events if e.get("finished")]

    return max(finished) if finished else 0


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
    end = max(min(last, live_gw), begin - 1)

    live_points = get_live_points(live_gw) if display_gw else {}

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
        gw_points = 0
        gw_hit = 0
        extra = 0     # live points not yet in FPL's history

        if display_gw:

            picks_data = get_manager_picks(entry, live_gw)

            hist_row = next(
                (g for g in history.get("current", [])
                 if g.get("event") == live_gw),
                None
            )

            if hist_row:
                gross = hist_row.get("points", 0)
                gw_hit = hist_row.get("event_transfers_cost", 0)
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

            # Green number = gameweek points AFTER the transfer hit
            gw_points = gross - gw_hit

            if not hist_row and first <= live_gw <= last:
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
            "movement": movement
        })

    players.sort(
        key=lambda x: (
            0 if x["league"] == "championship" else 1,
            x["rank"]
        )
    )

    snapshot = {
        "gameweek": gameweek,
        "live": gameweek > finished_gw,
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
            gw_text = f"+{player['gw_points']}"

            draw.text(
                (x + w - 115, row_y + 18),
                gw_text,
                fill=green,
                font=points_font
            )

            draw.text(
                (x + w - 115, row_y + 52),
                gw_label,
                fill=green,
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
            t for t in get_manager_transfers(entry_id)
            if t.get("event") == gameweek
        ]

        transfers.sort(key=lambda t: t.get("time", ""))

        transfer_rows = []

        for t in transfers:
            out_id = t["element_out"]
            in_id = t["element_in"]

            transfer_rows.append({
                "out": player_names.get(out_id, "Unknown"),
                "out_points": live_points.get(out_id, 0),
                "in": player_names.get(in_id, "Unknown"),
                "in_points": live_points.get(in_id, 0)
            })

        managers.append({
            "rank": row["league_rank"],
            "league": row["league"],
            "curries": row["curries"],
            "manager": row.get("player_name", "Unknown Manager"),
            "team": row.get("entry_name", "Unknown Team"),
            "hit_cost": hit_cost,
            "chips": chips,
            "transfers": transfer_rows
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
    height = 145 + 30 + content_h + 110

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

            draw.text(
                (x + 25, y + 48),
                m["team"][:34],
                fill=grey,
                font=small_font
            )

            # Hit cost / chip (top right)
            tag_y = y + 18

            if m["chips"]:
                right_text(
                    x + col_w - 25, tag_y,
                    " + ".join(m["chips"]),
                    green, small_font
                )
                tag_y += 26

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

                    row_y += transfer_h

            y += h + block_gap

    draw_column(40, left)
    draw_column(720, right)

    # Footer
    draw.text(
        (50, height - 85),
        f"Updated: {data['updated']}",
        fill=grey,
        font=small_font
    )
    draw.text(
        (50, height - 50),
        "Red = transferred out  •  Green = transferred in",
        fill=green,
        font=small_font
    )

    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)

    return output


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

@app.route("/api/hits", methods=["POST"])
def api_hits():
    """Password-protected hits record."""

    body = request.get_json(silent=True) or {}

    supplied = str(body.get("password", ""))

    if not hmac.compare_digest(
        supplied.encode("utf-8"),
        HITS_PASSWORD.encode("utf-8")
    ):
        time.sleep(1)    # slows down password guessing
        return jsonify({"error": "Wrong password"}), 401

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
   HITS TRACKER (password protected pop-up)
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
   FOOTER
   ========================================================== */

.footer {

    text-align: center;

    color: #557268;

    font-size: 12px;

    padding: 20px;
}

</style>

</head>


<body>


<div class="header">

    <h1>⚽ FPL LEAGUE</h1>

    <p id="leagueName">
        Loading league...
    </p>

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
    🔒 HITS TRACKER
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

        <h2>
            🧢 Captains This Gameweek
        </h2>

        <div id="captains" class="captains-grid"></div>

    </div>

</div>


<div id="hitsModal" class="modal"
     onclick="if (event.target === this) closeHits()">

    <div class="modal-box">

        <div class="modal-top">

            <h2>🤡 Hits Tracker</h2>

            <button class="close-btn" onclick="closeHits()">✕</button>

        </div>

        <div id="hitsLogin">

            <div>Enter your password to see the hits record.</div>

            <input type="password"
                   id="hitsPassword"
                   placeholder="Password"
                   autocomplete="off"
                   onkeydown="if (event.key === 'Enter') unlockHits()">

            <button onclick="unlockHits()">🔓 UNLOCK</button>

            <div id="hitsError" class="hits-error"></div>

        </div>

        <div id="hitsResults" style="display: none;"></div>

    </div>

</div>


<div class="footer">

    FPL League Dashboard • Automatically updated from FPL

</div>


<script>

let currentData = null;
let lastHitsData = null;


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

                <div class="gw">

                    +${player.gw_points}
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
                Team GW points: +${player.gw_points}
            </div>
        `;

        container.appendChild(card);
    });
}


/* ==========================================================
   HITS TRACKER (password protected)
   ========================================================== */

function openHits() {

    document.getElementById("hitsModal").style.display = "flex";

    document.getElementById("hitsLogin").style.display = "block";

    document.getElementById("hitsResults").style.display = "none";

    document.getElementById("hitsError").innerText = "";

    const input = document.getElementById("hitsPassword");

    input.value = "";

    input.focus();
}

function closeHits() {

    document.getElementById("hitsModal").style.display = "none";

    // Wipe the data from the page when closed
    document.getElementById("hitsResults").innerHTML = "";

    lastHitsData = null;
}

async function unlockHits() {

    const input = document.getElementById("hitsPassword");

    const error = document.getElementById("hitsError");

    error.innerText = "Checking...";

    try {

        const response = await fetch("/api/hits", {

            method: "POST",

            headers: { "Content-Type": "application/json" },

            body: JSON.stringify({ password: input.value })
        });

        const data = await response.json();

        if (!response.ok) {

            throw new Error(data.error || "Unable to unlock");
        }

        input.value = "";

        error.innerText = "";

        document.getElementById("hitsLogin").style.display = "none";

        document.getElementById("hitsResults").style.display = "block";

        renderHits(data);

    } catch (err) {

        error.innerText = err.message;
    }
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
   SECURITY
   ========================================================== */

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
   INITIAL LOAD
   ========================================================== */

refreshData();

</script>


</body>

</html>
"""


# ============================================================
# RUN SERVER
# ============================================================

if __name__ == "__main__":

    if HITS_PASSWORD == "change-me":
        print(
            "WARNING: change HITS_PASSWORD at the top of the file "
            "to protect the hits tracker."
        )

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )
