from flask import Flask, jsonify, render_template_string
import requests
import json
import os
from datetime import datetime

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

    color: white;

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
   CHART
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

.chart {

    height: 400px;

    overflow-x: auto;
}

.chart-inner {

    min-width: 900px;

    height: 100%;

    display: flex;

    align-items: flex-end;

    gap: 12px;

    padding:
        20px
        10px
        35px;
}

.bar-group {

    flex: 1;

    height: 100%;

    display: flex;

    align-items: flex-end;

    gap: 3px;
}

.bar {

    flex: 1;

    min-width: 7px;

    background:
        linear-gradient(
            180deg,
            #00ff87,
            #009f58
        );

    border-radius:
        5px
        5px
        0
        0;

    position: relative;
}

.bar-label {

    position: absolute;

    bottom: -25px;

    left: 50%;

    transform:
        translateX(-50%);

    font-size: 10px;

    color: #78968a;
}

.bar-value {

    position: absolute;

    top: -20px;

    left: 50%;

    transform:
        translateX(-50%);

    font-size: 10px;

    color: white;

    white-space: nowrap;
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
            📈 Points Progression
        </h2>

        <div class="chart">

            <div
                id="chart"
                class="chart-inner">
            </div>

        </div>

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


    drawChart();
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
   HISTORY CHART
   ========================================================== */

async function drawChart() {

    const response =
        await fetch(
            "/api/history"
        );

    const history =
        await response.json();


    const chart =
        document.getElementById(
            "chart"
        );

    chart.innerHTML = "";


    if (!history.length) {

        chart.innerHTML =
            "<p>No history yet.</p>";

        return;
    }


    const latest =
        history[
            history.length - 1
        ];


    const players =
        latest.players;


    /*
     * Create a group for each player.
     */

    players.forEach(
        player => {

            const group =
                document.createElement(
                    "div"
                );

            group.className =
                "bar-group";


            history.forEach(
                week => {

                    const record =
                        week.players.find(
                            p =>
                                p.entry ===
                                player.entry
                        );


                    if (!record)
                        return;


                    /*
                     * Scale the total points
                     * relative to the league.
                     */

                    const maxPoints =
                        Math.max(
                            ...week.players.map(
                                p =>
                                    p.total
                            )
                        );


                    const height =
                        Math.max(
                            5,
                            (
                                record.total /
                                maxPoints
                            ) * 100
                        );


                    const bar =
                        document.createElement(
                            "div"
                        );

                    bar.className =
                        "bar";


                    bar.style.height =
                        height + "%";


                    bar.title =
                        `${record.manager}
                         • GW${week.gameweek}
                         • ${record.total} points`;


                    const label =
                        document.createElement(
                            "div"
                        );

                    label.className =
                        "bar-label";

                    label.innerText =
                        `GW${week.gameweek}`;


                    bar.appendChild(
                        label
                    );


                    group.appendChild(
                        bar
                    );

                }
            );


            chart.appendChild(
                group
            );

        }
    );
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