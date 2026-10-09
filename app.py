from flask import Flask, jsonify, render_template_string, send_file, request
import requests
import json
import os
from datetime import datetime
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

# ============================================================
# SETTINGS
# ============================================================

LEAGUE_ID = 1459721
HISTORY_FILE = "fpl_history.json"

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


def build_snapshot():
    """Build a complete current league snapshot."""

    league_name, standings = get_league()

    gameweek = get_current_gameweek()

    # Fetched once per update, shared by every manager
    player_names = get_player_names()
    live_points = get_live_points(gameweek)

    previous = get_previous_snapshot()

    previous_players = {}

    if previous:
        for player in previous.get("players", []):
            previous_players[player["entry"]] = player

    players = []

    for row in standings:

        entry_id = row.get("entry")

        manager_name = row.get(
            "player_name",
            "Unknown Manager"
        )

        team_name = row.get(
            "entry_name",
            "Unknown Team"
        )

        rank = row.get(
            "rank",
            0
        )

        total = row.get(
            "total",
            0
        )

        # ----------------------------------------------------
        # Get Gameweek points
        # ----------------------------------------------------

        manager_history = get_manager_history(entry_id)

        current_history = manager_history.get(
            "current",
            []
        )

        gw_points = 0

        matching_gw = [
            x for x in current_history
            if x.get("event") == gameweek
        ]

        if matching_gw:
            gw_points = matching_gw[0].get(
                "points",
                0
            )

        # ----------------------------------------------------
        # Captain for this Gameweek
        # ----------------------------------------------------

        captain_name = "—"
        captain_points = 0

        picks_data = get_manager_picks(entry_id, gameweek)

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
        # Previous position
        # ----------------------------------------------------

        previous_rank = None

        if entry_id in previous_players:
            previous_rank = previous_players[
                entry_id
            ].get("rank")

        movement = 0

        if previous_rank is not None:
            movement = previous_rank - rank

        players.append({
            "entry": entry_id,
            "rank": rank,
            "manager": manager_name,
            "team": team_name,
            "total": total,
            "gw_points": gw_points,
            "captain": captain_name,
            "captain_points": captain_points,
            "previous_rank": previous_rank,
            "movement": movement
        })

    players.sort(
        key=lambda x: x["rank"]
    )

    snapshot = {
        "gameweek": gameweek,
        "league_name": league_name,
        "updated": datetime.now().strftime(
            "%d %b %Y %H:%M"
        ),
        "players": players
    }

    return snapshot

# ============================================================
# WHATSAPP GRAPHIC
# ============================================================

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
        f"{snapshot['league_name']}  •  GAMEWEEK {snapshot['gameweek']}",
        fill=dark,
        font=small_font
    )

    # Split league
    top5 = snapshot["players"][:5]
    bottom5 = snapshot["players"][5:10]

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

            # Manager
            manager = player["manager"]

            draw.text(
                (x + 95, row_y + 18),
                manager[:24],
                fill=white,
                font=name_font
            )

            # Team
            draw.text(
                (x + 95, row_y + 50),
                player["team"][:28],
                fill=grey,
                font=small_font
            )

            # Overall points
            draw.text(
                (x + w - 205, row_y + 18),
                str(player["total"]),
                fill=white,
                font=points_font
            )

            draw.text(
                (x + w - 205, row_y + 52),
                "TOTAL",
                fill=grey,
                font=small_font
            )

            # GW points
            gw_text = f"+{player['gw_points']} GW"

            draw.text(
                (x + w - 100, row_y + 35),
                gw_text,
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

    chip_labels = {
        "wildcard": "WILDCARD",
        "freehit": "FREE HIT",
        "bboost": "BENCH BOOST",
        "3xc": "TRIPLE CAPTAIN",
        "manager": "ASSISTANT MANAGER"
    }

    managers = []

    # Same top 10 as the dashboard
    for row in standings[:10]:

        entry_id = row.get("entry")

        history = get_manager_history(entry_id)

        hit_cost = 0
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
            "rank": row.get("rank", 0),
            "manager": row.get("player_name", "Unknown Manager"),
            "team": row.get("entry_name", "Unknown Team"),
            "hit_cost": hit_cost,
            "chips": chips,
            "transfers": transfer_rows
        })

    managers.sort(key=lambda m: m["rank"])

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
    left = managers[:5]
    right = managers[5:10]

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
            draw.text(
                (x + 25, y + 15),
                f"{m['rank']}. {m['manager'][:26]}",
                fill=white,
                font=name_font
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

        # Defaults to the latest finished Gameweek (same as the dashboard).
        # Use /download-transfers?gw=12 to pick a specific one.
        gameweek = request.args.get("gw", type=int)

        if not gameweek:
            gameweek = get_current_gameweek()

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
        80px
        85px;

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

</div>


<div class="container">

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


<div class="footer">

    FPL League Dashboard • Automatically updated from FPL

</div>


<script>

let currentData = null;


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
        `${data.league_name} • GAMEWEEK ${data.gameweek}`;


    const top5 =
        data.players.slice(
            0,
            5
        );

    const bottom5 =
        data.players.slice(
            5,
            10
        );


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


    players.forEach(
        player => {

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

                        ${movement}
                    </div>

                    <div class="team-name">
                        ${escapeHtml(
                            player.team
                        )}
                    </div>

                </div>

                <div class="total">

                    ${player.total}

                </div>

                <div class="gw">

                    +${player.gw_points}
                    <br>

                    <small>
                        GW
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

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )
