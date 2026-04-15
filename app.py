"""
Sports Tracker Web App  —  Multi-User + Notification Filters
=============================================================
Each user gets their own isolated session with:
  - Personal ntfy topic for push notifications
  - Per-sport notification type filters (all selected by default)
  - Filters are checked in the tracker thread before firing any alert

Deploy on Railway:
    Start command: python3 -m gunicorn --worker-class eventlet -w 1 app:app

Requirements:
    pip install flask flask-socketio requests MLB-StatsAPI eventlet gunicorn
"""

import os
import threading
import requests
import requests.packages.urllib3 as urllib3
from datetime import date, datetime
from flask import Flask, render_template_string, jsonify, request
from flask_socketio import SocketIO, emit, join_room

# ─────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────

SECRET_KEY    = os.environ.get("SECRET_KEY", "dev-secret-change-me")
POLL_INTERVAL = 30
PORT          = int(os.environ.get("PORT", 5000))

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_WEB  = "https://site.web.api.espn.com/apis/site/v2/sports"
MLB_LIVE  = "https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live"

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

sessions      = {}
sessions_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────
#  FILTER DEFINITIONS
#  These are the canonical category names shown in the UI.
#  Each sport maps raw API event strings → category name.
# ─────────────────────────────────────────────────────────────

FILTERS = {
    "mlb": [
        "Home Run",
        "Hit",           # single / double / triple
        "Out",           # groundout / flyout / lineout / popout
        "Strikeout",
        "Walk / HBP",
        "RBI",           # any play with rbi > 0
        "Scoring Play",  # any play where runs scored
    ],
    "nba": [
        "Points",        # made field goals + free throws
        "3-Pointer",
        "Rebound",
        "Assist",
        "Block",
        "Steal",
        "Turnover",
        "Foul",
        "Substitution",  # player enters or leaves the game
    ],
    "nhl": [
        "Goal",
        "Assist",
        "Shot",
        "Block",
        "Penalty",
        "Save",
        "Substitution",  # player enters or leaves the ice
    ],
    "pga": [
        "Eagle or better",
        "Birdie",
        "Par",
        "Bogey",
        "Double Bogey or worse",
        "Score change",   # catches anything not mapped above
        "Shot by Shot",   # per-hole notification as each hole completes
    ],
}

# ── MLB: map result.event strings → filter category ──────────
MLB_EVENT_MAP = {
    # Home runs
    "Home Run":                  "Home Run",
    # Hits
    "Single":                    "Hit",
    "Double":                    "Hit",
    "Triple":                    "Hit",
    # Outs
    "Groundout":                 "Out",
    "Ground Out":                "Out",
    "Flyout":                    "Out",
    "Fly Out":                   "Out",
    "Lineout":                   "Out",
    "Line Out":                  "Out",
    "Pop Out":                   "Out",
    "Forceout":                  "Out",
    "Double Play":               "Out",
    "Triple Play":               "Out",
    "Grounded Into DP":          "Out",
    "Fielders Choice Out":       "Out",
    "Fielders Choice":           "Out",
    "Bunt Groundout":            "Out",
    "Bunt Lineout":              "Out",
    "Bunt Pop Out":              "Out",
    "Sac Fly":                   "Out",
    "Sac Bunt":                  "Out",
    "Sac Fly Double Play":       "Out",
    # Strikeouts
    "Strikeout":                 "Strikeout",
    "Strikeout Double Play":     "Strikeout",
    # Walks / HBP
    "Walk":                      "Walk / HBP",
    "Intent Walk":               "Walk / HBP",
    "Hit By Pitch":              "Walk / HBP",
    # Misc
    "Field Error":               "Out",
    "Catcher Interference":      "Walk / HBP",
}

# ── NBA: map ESPN play type text → filter category ───────────
NBA_TYPE_MAP = {
    "Free Throw Made":           "Points",
    "Free Throw":                "Points",
    "Two-Point":                 "Points",
    "Layup":                     "Points",
    "Dunk":                      "Points",
    "Jump Shot":                 "Points",
    "Hook Shot":                 "Points",
    "Tip Shot":                  "Points",
    "Three Point":               "3-Pointer",
    "Three-Point":               "3-Pointer",
    "Rebound":                   "Rebound",
    "Offensive Rebound":         "Rebound",
    "Defensive Rebound":         "Rebound",
    "Assist":                    "Assist",
    "Block":                     "Block",
    "Steal":                     "Steal",
    "Turnover":                  "Turnover",
    "Foul":                      "Foul",
    "Shooting Foul":             "Foul",
    "Offensive Foul":            "Foul",
    "Loose Ball Foul":           "Foul",
    "Personal Foul":             "Foul",
    "Technical Foul":            "Foul",
    "Flagrant Foul":             "Foul",
}

# ── NHL: map ESPN play type text → filter category ───────────
NHL_TYPE_MAP = {
    "Goal":                      "Goal",
    "Power Play Goal":           "Goal",
    "Short Handed Goal":         "Goal",
    "Empty Net Goal":            "Goal",
    "Penalty Shot Goal":         "Goal",
    "Assist":                    "Assist",
    "Shot on Goal":              "Shot",
    "Shot":                      "Shot",
    "Missed Shot":               "Shot",
    "Blocked Shot":              "Block",
    "Block":                     "Block",
    "Penalty":                   "Penalty",
    "Minor Penalty":             "Penalty",
    "Major Penalty":             "Penalty",
    "Misconduct":                "Penalty",
    "Save":                      "Save",
    "Goalie Save":               "Save",
}


def mlb_categorize(event: str, rbi: int, runs_scored: int) -> list:
    """Return list of filter categories this MLB play belongs to."""
    cats = set()
    cat  = MLB_EVENT_MAP.get(event)
    if cat:
        cats.add(cat)
    else:
        # Unknown event — treat as Out if nothing else matches
        cats.add("Out")
    if rbi > 0:
        cats.add("RBI")
    if runs_scored > 0:
        cats.add("Scoring Play")
    return list(cats)


def espn_categorize(play_type: str, type_map: dict) -> str:
    """Map an ESPN play type string to a filter category."""
    # Exact match first
    if play_type in type_map:
        return type_map[play_type]
    # Partial match
    for key, cat in type_map.items():
        if key.lower() in play_type.lower() or play_type.lower() in key.lower():
            return cat
    return None


def pga_categorize(score_to_par: int) -> list:
    """Return PGA filter categories for a score relative to par."""
    cats = ["Score change"]
    if score_to_par <= -2:   cats.append("Eagle or better")
    elif score_to_par == -1: cats.append("Birdie")
    elif score_to_par == 0:  cats.append("Par")
    elif score_to_par == 1:  cats.append("Bogey")
    else:                    cats.append("Double Bogey or worse")
    return cats


def passes_filter(event_cats: list, active_filters: set) -> bool:
    """True if any of the event's categories are in the user's active filter set."""
    return bool(set(event_cats) & active_filters)


# ─────────────────────────────────────────────────────────────
#  PER-USER HELPERS
# ─────────────────────────────────────────────────────────────

def user_log(sid: str, msg: str):
    ts   = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(f"[{sid[:6]}] {line}")
    socketio.emit("log", {"text": line}, room=sid)


def user_alert(sid: str, player: str, event: str, detail: str, sport: str):
    socketio.emit("alert", {
        "player": player,
        "event":  event,
        "detail": detail,
        "sport":  sport,
        "time":   datetime.now().strftime("%H:%M:%S"),
    }, room=sid)


def user_notify(sid: str, title: str, message: str, priority: str = "default"):
    with sessions_lock:
        ntfy_url = sessions.get(sid, {}).get("ntfy_url")
    if not ntfy_url:
        return
    safe = title.encode("ascii", errors="replace").decode("ascii")
    try:
        requests.post(ntfy_url, data=message.encode("utf-8"),
                      headers={"Title": safe, "Priority": priority,
                               "Tags": "sports",
                               "Content-Type": "text/plain; charset=utf-8"},
                      timeout=10)
    except Exception as e:
        user_log(sid, f"Notification error: {e}")


def get_filters(sid: str) -> set:
    """Return the active filter set for this user's session."""
    with sessions_lock:
        return set(sessions.get(sid, {}).get("filters", []))


def stop_user_session(sid: str):
    with sessions_lock:
        session = sessions.get(sid)
    if session and session.get("stop"):
        session["stop"].set()
        t = session.get("thread")
        if t and t.is_alive():
            t.join(timeout=5)


# ─────────────────────────────────────────────────────────────
#  ESPN HELPERS
# ─────────────────────────────────────────────────────────────

def espn_get(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def espn_game_label(event):
    comps  = event.get("competitions", [{}])
    teams  = comps[0].get("competitors", []) if comps else []
    away   = next((t["team"].get("displayName","?") for t in teams if t.get("homeAway")=="away"), "Away")
    home   = next((t["team"].get("displayName","?") for t in teams if t.get("homeAway")=="home"), "Home")
    detail = event.get("status",{}).get("type",{}).get("shortDetail","")
    return f"{away} @ {home}", detail


def espn_score_str(summary):
    try:
        comps = summary.get("header",{}).get("competitions",[{}])
        teams = comps[0].get("competitors",[])
        return "  ".join(f"{t['team'].get('abbreviation','?')} {t.get('score','?')}" for t in teams)
    except Exception:
        return ""


def espn_is_final(summary):
    try:
        return (summary.get("header",{})
                       .get("competitions",[{}])[0]
                       .get("status",{})
                       .get("type",{})
                       .get("name","") == "STATUS_FINAL")
    except Exception:
        return False


def espn_participants(play):
    ids = []
    for p in play.get("participants", []):
        pid = str(p.get("athlete",{}).get("id","")).strip()
        if pid: ids.append(pid)
    direct = str(play.get("athlete",{}).get("id","")).strip()
    if direct and direct not in ids: ids.append(direct)
    return ids


def espn_roster(game_id, sport, league):
    summary = espn_get(f"{ESPN_WEB}/{sport}/{league}/summary", {"event": game_id})
    roster  = {}
    for team_box in summary.get("boxscore",{}).get("players",[]):
        for sg in team_box.get("statistics",[]):
            for entry in sg.get("athletes",[]):
                a = entry.get("athlete",{})
                n, pid = a.get("displayName",""), str(a.get("id","")).strip()
                if n and pid: roster[pid] = n
    if not roster:
        for tr in summary.get("rosters",[]):
            for entry in tr.get("roster",[]):
                a = entry.get("athlete",{})
                n, pid = a.get("displayName",""), str(a.get("id","")).strip()
                if n and pid: roster[pid] = n
    return roster


def espn_status_order(event):
    name = event.get("status",{}).get("type",{}).get("name","")
    if name == "STATUS_IN_PROGRESS": return 0
    if name == "STATUS_FINAL":       return 2
    return 1


# ─────────────────────────────────────────────────────────────
#  DATA API ROUTES
# ─────────────────────────────────────────────────────────────

@app.route("/api/games/<sport>")
def api_games(sport):
    today = date.today().strftime("%Y%m%d")
    if sport == "mlb":
        try:
            import statsapi
            games_raw = statsapi.schedule(date=date.today().strftime("%m/%d/%Y"))
            def sk(g):
                s = g.get("status","")
                if any(x in s for x in ("Progress","Live","Warmup","Manager")): return 0
                if any(x in s for x in ("Final","Over","Completed")):            return 2
                return 1
            games_raw.sort(key=sk)
            games = []
            for g in games_raw:
                k    = sk(g)
                away = g.get("away_name","Away")
                home = g.get("home_name","Home")
                if k == 0:   badge = f"LIVE  Inn {g.get('current_inning','?')}"
                elif k == 2: badge = f"Final  {g.get('away_score','?')}-{g.get('home_score','?')}"
                else:
                    dt = g.get("game_datetime","")
                    badge = (dt[11:16]+" UTC") if len(dt)>15 else "Scheduled"
                games.append({"id": g["game_id"], "label": f"{away} @ {home}",
                              "badge": badge, "live": k==0})
            return jsonify(games)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif sport == "pga":
        data   = espn_get(f"{ESPN_BASE}/golf/pga/scoreboard", {"limit": 10})
        events = data.get("events",[])
        return jsonify([{"id": e["id"], "label": e.get("name","Event"),
                         "badge": "", "live": True} for e in events])
    else:
        mapping = {"nba": ("basketball","nba"), "nhl": ("hockey","nhl")}
        sp, lg  = mapping.get(sport, (None, None))
        if not sp: return jsonify([])
        data   = espn_get(f"{ESPN_BASE}/{sp}/{lg}/scoreboard", {"dates": today, "limit": 20})
        events = data.get("events",[])
        events.sort(key=espn_status_order)
        games  = []
        for e in events:
            label, badge = espn_game_label(e)
            games.append({"id": e["id"], "label": label, "badge": badge,
                          "live": espn_status_order(e)==0})
        return jsonify(games)


@app.route("/api/players/<sport>/<game_id>")
def api_players(sport, game_id):
    if sport == "mlb":
        try:
            import statsapi
            boxscore = statsapi.boxscore_data(int(game_id))
            players  = {}
            for side in ("away","home"):
                for _, pd in boxscore.get(side,{}).get("players",{}).items():
                    n   = pd.get("person",{}).get("fullName","")
                    pid = pd.get("person",{}).get("id")
                    pos = pd.get("position",{}).get("abbreviation","")
                    if n and pid: players[str(pid)] = {"name": n, "pos": pos}
            return jsonify(players)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif sport == "pga":
        roster  = {}
        summary = espn_get(f"{ESPN_BASE}/golf/pga/summary", {"event": game_id})
        for comp in summary.get("competitions",[]):
            for c in comp.get("competitors",[]):
                a   = c.get("athlete",{})
                n   = a.get("displayName","") or a.get("fullName","")
                pid = str(a.get("id","")).strip()
                if n and pid: roster[pid] = {"name": n, "pos": ""}
        return jsonify(roster)
    else:
        mapping = {"nba": ("basketball","nba"), "nhl": ("hockey","nhl")}
        sp, lg  = mapping.get(sport, (None, None))
        if not sp: return jsonify({})
        raw = espn_roster(game_id, sp, lg)
        return jsonify({pid: {"name": name, "pos": ""} for pid, name in raw.items()})


@app.route("/api/filters/<sport>")
def api_filters(sport):
    """Return the filter options for a given sport."""
    return jsonify(FILTERS.get(sport, []))


# ─────────────────────────────────────────────────────────────
#  TRACKER THREADS
# ─────────────────────────────────────────────────────────────

def track_mlb(sid, game_id, player_ids, player_names, stop_event):
    import statsapi
    seen = set()
    user_log(sid, f"MLB tracker started — {', '.join(player_names.values())}")
    while not stop_event.is_set():
        try:
            today  = date.today().strftime("%m/%d/%Y")
            status = next((g.get("status","") for g in statsapi.schedule(date=today)
                           if g.get("game_id") == game_id), "")
            if any(x in status for x in ("Final","Over","Completed")):
                user_log(sid, "Game ended.")
                break

            url   = MLB_LIVE.format(pk=game_id)
            data  = requests.get(url, timeout=20).json()
            plays = data.get("liveData",{}).get("plays",{}).get("allPlays",[])
            ls    = data.get("liveData",{}).get("linescore",{})
            ar    = ls.get("teams",{}).get("away",{}).get("runs","?")
            hr    = ls.get("teams",{}).get("home",{}).get("runs","?")
            inn   = ls.get("currentInning","?")
            half  = ls.get("inningHalf","")
            score = f"{ar}-{hr} ({half} {inn})"

            active_filters = get_filters(sid)
            new = 0
            for play in plays:
                about    = play.get("about",{})
                ab_index = about.get("atBatIndex")
                if not about.get("isComplete", False): continue
                if ab_index in seen: continue
                seen.add(ab_index)

                batter_id = play.get("matchup",{}).get("batter",{}).get("id")
                if batter_id is None: continue
                batter_id = int(batter_id)
                if batter_id not in player_ids: continue

                result      = play.get("result",{})
                event       = result.get("event","At-bat")
                desc        = result.get("description","") or event
                rbi         = result.get("rbi", 0)
                runs_scored = result.get("runs", 0)
                h           = "Top" if about.get("halfInning")=="top" else "Bot"
                inn_n       = about.get("inning","?")
                extra       = f" ({rbi} RBI)" if rbi else ""
                detail      = f"{h} {inn_n} | Score: {score}\n{desc}{extra}"
                name        = player_names[batter_id]

                # Filter check
                event_cats = mlb_categorize(event, rbi, runs_scored)
                if not passes_filter(event_cats, active_filters):
                    user_log(sid, f"Filtered out: {name} — {event} {event_cats}")
                    continue

                user_log(sid, f"{name}: {event}")
                user_alert(sid, name, event, detail, "MLB")
                user_notify(sid, f"MLB - {name}: {event}", detail, "high")
                new += 1

            user_log(sid, f"{len(plays)} plays checked, {new} new alerts")
        except Exception as e:
            user_log(sid, f"Error: {e}")
        stop_event.wait(POLL_INTERVAL)
    user_log(sid, "MLB tracker stopped.")


def is_substitution_play(play_type: str, sport_label: str) -> bool:
    """Detect substitution/line-change plays for NBA and NHL."""
    pt = play_type.lower()
    if sport_label == "NBA":
        return "substitution" in pt
    if sport_label == "NHL":
        # NHL uses "On Ice" / "Off Ice" or "Substitution"
        return any(x in pt for x in ("substitution", "on ice", "off ice", "line change"))
    return False


def parse_substitution(play: dict, player_ids: set, player_names: dict, sport_label: str):
    """
    Return list of (name, action) tuples for tracked players involved in a sub.
    ESPN puts both the incoming and outgoing player in participants with a
    'role' field ("enteredGame" / "leftGame") when available.
    Falls back to position in list: first = entering, second = leaving.
    """
    results = []
    participants = play.get("participants", [])
    for i, p in enumerate(participants):
        pid  = str(p.get("athlete", {}).get("id", "")).strip()
        role = p.get("type", {}).get("text", "").lower()  # "entered game" / "left game"
        if pid not in player_ids:
            continue
        name = player_names[pid]
        if "enter" in role or "in" in role:
            action = "Entered the game"
        elif "left" in role or "exit" in role or "out" in role:
            action = "Left the game"
        else:
            # Fallback: ESPN sometimes puts entrant first
            action = "Entered the game" if i == 0 else "Left the game"
        results.append((name, action))
    return results


def track_espn(sid, sport, league, sport_label, game_id, title, player_ids, player_names, stop_event):
    type_map = NBA_TYPE_MAP if sport_label == "NBA" else NHL_TYPE_MAP
    seen     = set()
    user_log(sid, f"{sport_label} tracker started — {', '.join(player_names.values())}")
    while not stop_event.is_set():
        try:
            summary = espn_get(f"{ESPN_WEB}/{sport}/{league}/summary", {"event": game_id})
            if espn_is_final(summary):
                score = espn_score_str(summary)
                user_log(sid, f"Game ended. Final: {score}")
                user_notify(sid, f"{sport_label} Final: {title}", score, "low")
                break

            plays          = summary.get("plays",[])
            score          = espn_score_str(summary)
            active_filters = get_filters(sid)
            new            = 0

            for play in plays:
                play_id = str(play.get("id","")).strip()
                if not play_id or play_id in seen: continue
                seen.add(play_id)

                play_type = play.get("type",{}).get("text","Play")
                period    = play.get("period",{}).get("number","?")
                clock     = play.get("clock",{}).get("displayValue","")
                time_str  = f"Q{period} {clock}" if sport_label=="NBA" else f"P{period} {clock}"
                text      = play.get("text","").strip()

                # ── Substitution path ────────────────────────
                if is_substitution_play(play_type, sport_label):
                    if "Substitution" not in active_filters:
                        continue
                    subs = parse_substitution(play, player_ids, player_names, sport_label)
                    for name, action in subs:
                        detail = f"{time_str} | {score}\n{action}"
                        user_log(sid, f"{name}: {action}")
                        user_alert(sid, name, action, detail, sport_label)
                        user_notify(sid, f"{sport_label} - {name}: {action}", detail, "default")
                        new += 1
                    continue

                # ── Normal play path ─────────────────────────
                pids     = espn_participants(play)
                matching = [p for p in pids if p in player_ids]
                if not matching: continue

                detail    = f"{time_str} | {score}\n{text or play_type}"

                event_cat = espn_categorize(play_type, type_map)
                if event_cat and not passes_filter([event_cat], active_filters):
                    continue

                for pid in matching:
                    name = player_names[pid]
                    user_log(sid, f"{name}: {text[:60]}")
                    user_alert(sid, name, play_type, detail, sport_label)
                    user_notify(sid, f"{sport_label} - {name}: {play_type}", detail, "high")
                    new += 1

            user_log(sid, f"{len(plays)} plays checked, {new} new alerts")
        except Exception as e:
            user_log(sid, f"Error: {e}")
        stop_event.wait(POLL_INTERVAL)
    user_log(sid, f"{sport_label} tracker stopped.")


PAR_LABELS = {-3:"Albatross",-2:"Eagle",-1:"Birdie",0:"Par",1:"Bogey",2:"Dbl Bogey"}

def hole_result_label(strokes: int, par: int) -> str:
    """Return a human label for a hole result."""
    diff = strokes - par
    if diff <= -3: return "Albatross or better"
    return PAR_LABELS.get(diff, f"+{diff}" if diff > 0 else str(diff))

def hole_filter_cat(strokes: int, par: int) -> str:
    """Map a hole result to a PGA filter category."""
    diff = strokes - par
    if diff <= -2: return "Eagle or better"
    if diff == -1: return "Birdie"
    if diff ==  0: return "Par"
    if diff ==  1: return "Bogey"
    return "Double Bogey or worse"


def track_pga(sid, event_id, event_name, player_ids, player_names, stop_event):
    last_snapshot  = {}   # pid -> snapshot string for overall score change alerts
    last_hole_idx  = {}   # pid -> number of completed holes seen last poll
    user_log(sid, f"PGA tracker started — {', '.join(player_names.values())}")
    while not stop_event.is_set():
        try:
            summary     = espn_get(f"{ESPN_BASE}/golf/pga/summary", {"event": event_id})
            competitors = []
            for comp in summary.get("competitions",[]):
                competitors.extend(comp.get("competitors",[]))

            active_filters = get_filters(sid)
            shot_by_shot   = "Shot by Shot" in active_filters
            new = 0
            for c in competitors:
                athlete = c.get("athlete",{})
                pid     = str(athlete.get("id","")).strip()
                if pid not in player_ids: continue

                name   = player_names[pid]
                score  = str(c.get("score","")).strip()
                status = str(c.get("status","")).strip()
                rounds = [str(r.get("displayValue","")) for r in c.get("rounds",[])]
                cur_rnd = rounds[-1] if rounds else "?"

                # ── Shot-by-shot: per-hole linescores ────────
                # linescores is a flat list of every hole played this round.
                # Each entry: {"value": strokes, "par": par_value, ...}
                linescores = c.get("linescores", [])
                completed  = [l for l in linescores
                              if l.get("value") not in ("", "-", None, 0)
                              and l.get("par") not in ("", None)]
                prev_count = last_hole_idx.get(pid, 0)

                if shot_by_shot and len(completed) > prev_count:
                    # Fire a notification for each newly completed hole
                    for i in range(prev_count, len(completed)):
                        hole_ls  = completed[i]
                        hole_num = hole_ls.get("number", i + 1)
                        try:
                            strokes = int(hole_ls.get("value", 0))
                            par     = int(hole_ls.get("par", 0))
                        except (ValueError, TypeError):
                            continue
                        label  = hole_result_label(strokes, par)
                        detail = (f"Hole {hole_num} — Par {par}\n"
                                  f"{strokes} stroke{'s' if strokes != 1 else ''} ({label})\n"
                                  f"Round total: {cur_rnd}  |  Tournament: {score}")
                        # Check the hole result passes score filter too
                        hole_cat = hole_filter_cat(strokes, par)
                        # Shot-by-shot fires regardless of score filters;
                        # it is its own filter opt-in
                        user_log(sid, f"{name}: Hole {hole_num} — {label} ({strokes}/{par})")
                        user_alert(sid, name, f"Hole {hole_num}: {label}", detail, "PGA")
                        user_notify(sid, f"PGA - {name}: Hole {hole_num} {label}", detail, "high")
                        new += 1

                last_hole_idx[pid] = len(completed)

                # ── Overall score change alert ────────────────
                thru     = str(len(completed))
                snapshot = f"{score}|{thru}|{cur_rnd}|{status}"
                if last_snapshot.get(pid) == snapshot:
                    continue
                last_snapshot[pid] = snapshot

                # Don't double-fire if shot-by-shot already covered this
                if shot_by_shot and len(completed) > prev_count:
                    continue

                try:
                    score_int = int(score.replace("E","0").replace("+",""))
                except Exception:
                    score_int = None

                event_cats = pga_categorize(score_int) if score_int is not None else ["Score change"]
                if not passes_filter(event_cats, active_filters):
                    continue

                rounds_str = "  ".join(f"R{i+1}:{s}" for i,s in enumerate(rounds))
                detail     = f"Score: {score}  Thru: {thru}\nStatus: {status}\n{rounds_str}".strip()
                user_log(sid, f"{name}: Score={score} Thru={thru}")
                user_alert(sid, name, f"Score: {score}", detail, "PGA")
                user_notify(sid, f"PGA - {name}: {score}", detail, "high")
                new += 1

            if espn_is_final(summary):
                user_log(sid, "Tournament complete.")
                break

            user_log(sid, f"{len(competitors)} players checked, {new} score updates")
        except Exception as e:
            user_log(sid, f"Error: {e}")
        stop_event.wait(POLL_INTERVAL)
    user_log(sid, "PGA tracker stopped.")


# ─────────────────────────────────────────────────────────────
#  SOCKET EVENTS
# ─────────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    sid = request.sid
    join_room(sid)
    with sessions_lock:
        sessions[sid] = {"thread": None, "stop": None, "ntfy_url": None, "filters": []}
    print(f"[+] Connected: {sid[:8]}")


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    stop_user_session(sid)
    with sessions_lock:
        sessions.pop(sid, None)
    print(f"[-] Disconnected: {sid[:8]}")


@socketio.on("set_ntfy_topic")
def on_set_ntfy(data):
    sid   = request.sid
    topic = data.get("topic","").strip()
    if not topic:
        emit("ntfy_status", {"ok": False, "msg": "Topic cannot be empty."})
        return
    ntfy_url = f"https://ntfy.sh/{topic}"
    with sessions_lock:
        if sid in sessions:
            sessions[sid]["ntfy_url"] = ntfy_url
    try:
        requests.post(ntfy_url,
                      data="Sports Tracker connected! You will receive alerts here.".encode(),
                      headers={"Title": "Sports Tracker Ready", "Tags": "white_check_mark",
                               "Content-Type": "text/plain; charset=utf-8"},
                      timeout=10)
        emit("ntfy_status", {"ok": True, "msg": f"Connected to topic: {topic}"})
    except Exception as e:
        emit("ntfy_status", {"ok": False, "msg": f"Could not reach ntfy.sh: {e}"})


@socketio.on("start_tracking")
def on_start_tracking(data):
    sid        = request.sid
    sport      = data.get("sport","").lower()
    game_id    = data.get("game_id")
    game_label = data.get("game_label","Game")
    players    = data.get("players",{})
    filters    = data.get("filters", FILTERS.get(sport, []))  # default = all

    if not game_id or not players:
        emit("log", {"text": "No game or players selected."})
        return

    stop_user_session(sid)
    stop_event = threading.Event()

    with sessions_lock:
        if sid in sessions:
            sessions[sid]["stop"]    = stop_event
            sessions[sid]["filters"] = filters  # store active filter set

    user_log(sid, f"Active filters: {', '.join(filters)}")
    user_notify(sid, f"Tracking {game_label}",
                f"Watching: {', '.join(players.values())}", "low")

    if sport == "mlb":
        pid_map = {int(k): v for k, v in players.items()}
        t = threading.Thread(target=track_mlb,
                             args=(sid, int(game_id), set(pid_map.keys()), pid_map, stop_event),
                             daemon=True)

    elif sport in ("nba","nhl"):
        sport_map = {"nba": ("basketball","nba","NBA"), "nhl": ("hockey","nhl","NHL")}
        sp, lg, label = sport_map[sport]
        t = threading.Thread(target=track_espn,
                             args=(sid, sp, lg, label, game_id, game_label,
                                   set(players.keys()), players, stop_event),
                             daemon=True)

    elif sport == "pga":
        t = threading.Thread(target=track_pga,
                             args=(sid, game_id, game_label,
                                   set(players.keys()), players, stop_event),
                             daemon=True)
    else:
        emit("log", {"text": f"Unknown sport: {sport}"}); return

    with sessions_lock:
        if sid in sessions:
            sessions[sid]["thread"] = t
    t.start()
    emit("tracking_started", {"game": game_label, "players": list(players.values()),
                               "filters": filters})


@socketio.on("stop_tracking")
def on_stop_tracking():
    sid = request.sid
    stop_user_session(sid)
    emit("log", {"text": "Tracker stopped."})


# ─────────────────────────────────────────────────────────────
#  UI
# ─────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Sports Tracker</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@400;600;700;900&family=Barlow:wght@400;500&display=swap');
  :root {
    --bg:#0a0e17; --surface:#111827; --card:#1a2235; --border:#2a3a55;
    --accent:#f59e0b; --accent2:#3b82f6; --green:#10b981; --red:#ef4444;
    --text:#e2e8f0; --muted:#64748b;
  }
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--text);font-family:'Barlow',sans-serif;min-height:100vh;padding-bottom:2rem;}
  header{background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);border-bottom:2px solid var(--accent);padding:1.2rem 1.5rem;display:flex;align-items:center;gap:1rem;}
  header h1{font-family:'Barlow Condensed',sans-serif;font-weight:900;font-size:1.6rem;letter-spacing:.05em;text-transform:uppercase;color:#fff;}
  header h1 span{color:var(--accent);}
  .dot{width:10px;height:10px;border-radius:50%;background:var(--muted);flex-shrink:0;transition:background .3s;}
  .dot.live{background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 1.5s infinite;}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .container{max-width:680px;margin:0 auto;padding:1.5rem 1rem;}
  .step{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.2rem;margin-bottom:1rem;transition:opacity .3s;}
  .step.disabled{opacity:.35;pointer-events:none;}
  .step-label{font-family:'Barlow Condensed',sans-serif;font-size:.75rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);margin-bottom:.7rem;}
  .btn-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:.5rem;}
  .btn-grid.tight{grid-template-columns:repeat(auto-fill,minmax(110px,1fr));}
  button{background:var(--card);border:1.5px solid var(--border);color:var(--text);border-radius:8px;padding:.65rem .8rem;font-family:'Barlow',sans-serif;font-size:.88rem;font-weight:500;cursor:pointer;text-align:left;transition:border-color .15s,background .15s,transform .1s;line-height:1.3;}
  button:active{transform:scale(.97);}
  button:hover{border-color:var(--accent2);background:#1f3050;}
  button.selected{border-color:var(--accent);background:#2a1f00;color:var(--accent);}
  button.player-btn.selected{border-color:var(--green);background:#0d2a1f;color:var(--green);}
  button.game-btn .badge{display:block;font-size:.72rem;color:var(--muted);margin-top:2px;}
  button.game-btn.live-game{border-color:#16a34a;}
  button.game-btn.live-game .badge{color:var(--green);}

  /* Filter chips */
  .filter-grid{display:flex;flex-wrap:wrap;gap:.4rem;}
  .filter-chip{display:inline-flex;align-items:center;gap:.35rem;background:var(--card);border:1.5px solid var(--green);color:var(--green);border-radius:20px;padding:.35rem .75rem;font-size:.8rem;font-weight:600;cursor:pointer;transition:background .15s,color .15s,border-color .15s;user-select:none;}
  .filter-chip:hover{background:#0d2a1f;}
  .filter-chip.off{border-color:var(--border);color:var(--muted);background:var(--card);}
  .filter-chip.off:hover{border-color:var(--accent2);color:var(--text);}
  .filter-chip .chip-dot{width:7px;height:7px;border-radius:50%;background:var(--green);flex-shrink:0;}
  .filter-chip.off .chip-dot{background:var(--muted);}
  .filter-actions{display:flex;gap:.5rem;margin-bottom:.6rem;}
  .filter-actions button{font-size:.75rem;padding:.3rem .7rem;border-radius:6px;color:var(--muted);}
  .filter-actions button:hover{color:var(--text);}

  .action-row{display:flex;gap:.6rem;margin-top:.5rem;}
  .btn-start{flex:1;background:var(--accent);border-color:var(--accent);color:#000;font-weight:700;font-family:'Barlow Condensed',sans-serif;font-size:1rem;letter-spacing:.05em;text-transform:uppercase;text-align:center;padding:.8rem;border-radius:8px;}
  .btn-start:hover{background:#d97706;border-color:#d97706;color:#000;}
  .btn-start:disabled{opacity:.4;cursor:not-allowed;}
  .btn-stop{background:transparent;border-color:var(--red);color:var(--red);font-weight:600;text-align:center;padding:.8rem 1.2rem;border-radius:8px;}
  .btn-stop:hover{background:#2a1010;}
  .selection-summary{font-size:.82rem;color:var(--muted);margin-top:.6rem;min-height:1.2em;}
  .selection-summary strong{color:var(--text);}
  .ntfy-row{display:flex;gap:.5rem;align-items:stretch;}
  .ntfy-row input,.search-wrap input{background:var(--card);border:1.5px solid var(--border);border-radius:8px;padding:.6rem .8rem;color:var(--text);font-family:'Barlow',sans-serif;font-size:.9rem;outline:none;}
  .ntfy-row input{flex:1;}
  .ntfy-row input:focus,.search-wrap input:focus{border-color:var(--accent2);}
  .ntfy-row input::placeholder,.search-wrap input::placeholder{color:var(--muted);}
  .btn-ntfy{background:var(--accent2);border-color:var(--accent2);color:#fff;font-weight:600;font-size:.85rem;padding:.6rem 1rem;border-radius:8px;text-align:center;white-space:nowrap;}
  .btn-ntfy:hover{background:#2563eb;border-color:#2563eb;}
  .ntfy-status{font-size:.8rem;margin-top:.5rem;min-height:1.2em;}
  .ntfy-status.ok{color:var(--green);}
  .ntfy-status.err{color:var(--red);}
  .search-wrap{position:relative;margin-bottom:.6rem;}
  .search-wrap input{width:100%;}
  .feed-panel{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden;margin-bottom:1rem;}
  .feed-header{background:var(--card);padding:.7rem 1rem;font-family:'Barlow Condensed',sans-serif;font-size:.8rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);display:flex;justify-content:space-between;align-items:center;}
  #status-badge{font-size:.72rem;background:var(--bg);border-radius:4px;padding:2px 7px;color:var(--muted);}
  #status-badge.active{color:var(--green);border:1px solid var(--green);}
  #alerts-list{padding:.5rem;}
  .alert-card{background:var(--card);border-left:3px solid var(--accent);border-radius:6px;padding:.7rem .9rem;margin-bottom:.4rem;animation:slideIn .3s ease;}
  .alert-card.nba{border-color:#f97316;}
  .alert-card.nhl{border-color:#a78bfa;}
  .alert-card.mlb{border-color:var(--accent);}
  .alert-card.pga{border-color:var(--green);}
  @keyframes slideIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
  .alert-player{font-family:'Barlow Condensed',sans-serif;font-size:1rem;font-weight:700;color:#fff;}
  .alert-event{font-size:.78rem;color:var(--accent);font-weight:600;margin:1px 0;}
  .alert-detail{font-size:.8rem;color:var(--muted);white-space:pre-line;}
  .alert-time{font-size:.7rem;color:var(--border);float:right;}
  #log-panel{background:#070b12;border:1px solid var(--border);border-radius:8px;padding:.6rem .8rem;font-family:'Barlow Condensed',monospace;font-size:.78rem;color:var(--muted);max-height:130px;overflow-y:auto;}
  #log-panel div{margin-bottom:1px;}
  .empty-state{text-align:center;padding:2rem 1rem;color:var(--muted);font-size:.88rem;}
  .loader{display:inline-block;width:14px;height:14px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-left:6px;}
  @keyframes spin{to{transform:rotate(360deg)}}
  .active-filters-summary{font-size:.78rem;color:var(--muted);margin-top:.5rem;}
  .active-filters-summary strong{color:var(--green);}
</style>
</head>
<body>
<header>
  <div class="dot" id="conn-dot"></div>
  <h1>Sports <span>Tracker</span></h1>
</header>
<div class="container">

  <!-- STEP 0: NTFY -->
  <div class="step" id="step-ntfy">
    <div class="step-label">Step 1 — Connect Your iPhone Notifications</div>
    <p style="font-size:.83rem;color:var(--muted);margin-bottom:.7rem;">Install the free <strong style="color:var(--text)">ntfy</strong> app on your iPhone, subscribe to any topic name you choose, then enter it below.</p>
    <div class="ntfy-row">
      <input type="text" id="ntfy-input" placeholder="your-topic-name" autocapitalize="none" autocorrect="off">
      <button class="btn-ntfy" onclick="connectNtfy()">Connect</button>
    </div>
    <div class="ntfy-status" id="ntfy-status"></div>
  </div>

  <!-- STEP 1: SPORT -->
  <div class="step disabled" id="step-sport">
    <div class="step-label">Step 2 — Select Sport</div>
    <div class="btn-grid">
      <button onclick="selectSport('mlb', this)">⚾ MLB</button>
      <button onclick="selectSport('nba', this)">🏀 NBA</button>
      <button onclick="selectSport('nhl', this)">🏒 NHL</button>
      <button onclick="selectSport('pga', this)">⛳ PGA Tour</button>
    </div>
  </div>

  <!-- STEP 2: GAME -->
  <div class="step disabled" id="step-game">
    <div class="step-label">Step 3 — Select Game / Event</div>
    <div id="games-list"><div class="empty-state">Select a sport first</div></div>
    <div class="selection-summary" id="game-summary"></div>
  </div>

  <!-- STEP 3: PLAYERS -->
  <div class="step disabled" id="step-players">
    <div class="step-label">Step 4 — Select Players</div>
    <div class="search-wrap">
      <input type="text" id="player-search" placeholder="Search players..." oninput="filterPlayers()">
    </div>
    <div class="btn-grid tight" id="players-list"><div class="empty-state">Select a game first</div></div>
    <div class="selection-summary" id="player-summary"></div>
  </div>

  <!-- STEP 4: FILTERS -->
  <div class="step disabled" id="step-filters">
    <div class="step-label">Step 5 — Notification Filters</div>
    <p style="font-size:.82rem;color:var(--muted);margin-bottom:.7rem;">Choose which events trigger a notification. All are on by default — tap to toggle off.</p>
    <div class="filter-actions">
      <button onclick="setAllFilters(true)">Select All</button>
      <button onclick="setAllFilters(false)">Clear All</button>
    </div>
    <div class="filter-grid" id="filter-chips"></div>
    <div class="active-filters-summary" id="filter-summary"></div>
  </div>

  <!-- STEP 5: START -->
  <div class="step disabled" id="step-start">
    <div class="step-label">Step 6 — Start Tracking</div>
    <div class="action-row">
      <button class="btn-start" id="btn-start" onclick="startTracking()" disabled>Start Tracking</button>
      <button class="btn-stop" onclick="stopTracking()">Stop</button>
    </div>
  </div>

  <!-- LIVE FEED -->
  <div class="feed-panel" id="feed-panel" style="display:none">
    <div class="feed-header">Live Alerts <span id="status-badge">Idle</span></div>
    <div id="alerts-list"><div class="empty-state" style="padding:1.5rem">Waiting for plays...</div></div>
  </div>

  <!-- LOG -->
  <div class="feed-panel" id="log-panel-wrap" style="display:none">
    <div class="feed-header">System Log</div>
    <div id="log-panel"></div>
  </div>

</div>
<script>
const socket = io();
let ntfyConnected   = false;
let selectedSport   = null;
let selectedGame    = null;
let selectedPlayers = {};
let allPlayers      = {};
let allFilters      = [];    // full list for current sport
let activeFilters   = new Set();  // currently ON filters

// ── Connection ───────────────────────────────────────────────
socket.on("connect",    () => document.getElementById("conn-dot").classList.add("live"));
socket.on("disconnect", () => document.getElementById("conn-dot").classList.remove("live"));
socket.on("log",   data => appendLog(data.text));
socket.on("alert", data => {
  document.getElementById("feed-panel").style.display = "block";
  const list  = document.getElementById("alerts-list");
  const empty = list.querySelector(".empty-state");
  if (empty) empty.remove();
  const card = document.createElement("div");
  card.className = `alert-card ${data.sport.toLowerCase()}`;
  card.innerHTML = `<span class="alert-time">${data.time}</span><div class="alert-player">${data.player}</div><div class="alert-event">${data.event}</div><div class="alert-detail">${data.detail}</div>`;
  list.insertBefore(card, list.firstChild);
  while (list.children.length > 30) list.removeChild(list.lastChild);
});
socket.on("tracking_started", data => {
  document.getElementById("status-badge").textContent = "LIVE";
  document.getElementById("status-badge").classList.add("active");
  document.getElementById("feed-panel").style.display = "block";
  document.getElementById("log-panel-wrap").style.display = "block";
  appendLog(`Tracking: ${data.game} | ${data.players.join(", ")} | Filters: ${data.filters.join(", ")}`);
});
socket.on("ntfy_status", data => {
  const el = document.getElementById("ntfy-status");
  el.textContent = data.msg;
  el.className   = "ntfy-status " + (data.ok ? "ok" : "err");
  if (data.ok) { ntfyConnected = true; setStep("step-sport", false); }
});

// ── Step 0: ntfy ─────────────────────────────────────────────
function connectNtfy() {
  const topic = document.getElementById("ntfy-input").value.trim();
  if (!topic) { document.getElementById("ntfy-status").textContent = "Please enter a topic name."; return; }
  document.getElementById("ntfy-status").className = "ntfy-status";
  document.getElementById("ntfy-status").textContent = "Connecting...";
  socket.emit("set_ntfy_topic", { topic });
}

// ── Step 1: Sport ─────────────────────────────────────────────
function selectSport(sport, btn) {
  selectedSport = sport; selectedGame = null; selectedPlayers = {};
  allPlayers = {}; allFilters = []; activeFilters = new Set();
  document.querySelectorAll("#step-sport button").forEach(b => b.classList.remove("selected"));
  btn.classList.add("selected");
  setStep("step-game", false);
  setStep("step-players", true);
  setStep("step-filters", true);
  setStep("step-start", true);
  document.getElementById("games-list").innerHTML = '<div class="empty-state"><span class="loader"></span> Loading...</div>';
  document.getElementById("players-list").innerHTML = '<div class="empty-state">Select a game first</div>';
  document.getElementById("filter-chips").innerHTML = '';
  document.getElementById("filter-summary").textContent = '';
  document.getElementById("game-summary").textContent = "";
  document.getElementById("player-summary").textContent = "";
  updateStartBtn();

  fetch(`/api/games/${sport}`)
    .then(r => r.json())
    .then(games => {
      const list = document.getElementById("games-list");
      if (!games.length) { list.innerHTML = '<div class="empty-state">No games today</div>'; return; }
      list.innerHTML = "";
      games.forEach(g => {
        const b = document.createElement("button");
        b.className = "game-btn" + (g.live ? " live-game" : "");
        b.innerHTML = `${g.label}<span class="badge">${g.badge}</span>`;
        b.onclick   = () => selectGame(g, b);
        list.appendChild(b);
      });
    })
    .catch(() => document.getElementById("games-list").innerHTML = '<div class="empty-state">Failed to load games</div>');
}

// ── Step 2: Game ──────────────────────────────────────────────
function selectGame(game, btn) {
  selectedGame = game; selectedPlayers = {}; allPlayers = {};
  document.querySelectorAll(".game-btn").forEach(b => b.classList.remove("selected"));
  btn.classList.add("selected");
  document.getElementById("game-summary").innerHTML = `<strong>${game.label}</strong>`;
  document.getElementById("players-list").innerHTML = '<div class="empty-state"><span class="loader"></span> Loading players...</div>';
  document.getElementById("player-summary").textContent = "";
  updateStartBtn();

  fetch(`/api/players/${selectedSport}/${game.id}`)
    .then(r => r.json())
    .then(players => {
      if (players.error || !Object.keys(players).length) {
        document.getElementById("players-list").innerHTML = '<div class="empty-state">No players found — game may not have started yet</div>';
        setStep("step-players", false);
        return;
      }
      allPlayers = players;
      setStep("step-players", false);
      renderPlayers(players);
      loadFilters();  // load filters once sport is confirmed
    })
    .catch(() => {
      document.getElementById("players-list").innerHTML = '<div class="empty-state">Failed to load players</div>';
      setStep("step-players", false);
    });
}

function renderPlayers(players) {
  const list = document.getElementById("players-list");
  const q    = document.getElementById("player-search").value.toLowerCase();
  list.innerHTML = "";
  const entries = Object.entries(players)
    .filter(([id, p]) => p.name.toLowerCase().includes(q))
    .sort((a, b) => a[1].name.localeCompare(b[1].name));
  if (!entries.length) { list.innerHTML = '<div class="empty-state">No players found</div>'; return; }
  entries.forEach(([id, p]) => {
    const b = document.createElement("button");
    b.className   = "player-btn" + (selectedPlayers[id] ? " selected" : "");
    b.textContent = p.name + (p.pos ? ` (${p.pos})` : "");
    b.dataset.id  = id;
    b.onclick     = () => togglePlayer(id, p.name, b);
    list.appendChild(b);
  });
}

function filterPlayers() { renderPlayers(allPlayers); }

function togglePlayer(id, name, btn) {
  if (selectedPlayers[id]) { delete selectedPlayers[id]; btn.classList.remove("selected"); }
  else                      { selectedPlayers[id] = name;  btn.classList.add("selected"); }
  const count = Object.keys(selectedPlayers).length;
  document.getElementById("player-summary").innerHTML =
    count ? `<strong>${count}</strong> player${count>1?"s":""} selected: ${Object.values(selectedPlayers).join(", ")}` : "";
  updateStartBtn();
}

// ── Step 4: Filters ───────────────────────────────────────────
function loadFilters() {
  fetch(`/api/filters/${selectedSport}`)
    .then(r => r.json())
    .then(filters => {
      allFilters    = filters;
      activeFilters = new Set(filters);  // all ON by default
      setStep("step-filters", false);
      renderFilterChips();
    });
}

function renderFilterChips() {
  const container = document.getElementById("filter-chips");
  container.innerHTML = "";
  allFilters.forEach(f => {
    const chip = document.createElement("div");
    const on   = activeFilters.has(f);
    chip.className   = "filter-chip" + (on ? "" : " off");
    chip.innerHTML   = `<span class="chip-dot"></span>${f}`;
    chip.onclick     = () => toggleFilter(f, chip);
    container.appendChild(chip);
  });
  updateFilterSummary();
  updateStartBtn();
}

function toggleFilter(name, chip) {
  if (activeFilters.has(name)) {
    activeFilters.delete(name);
    chip.classList.add("off");
  } else {
    activeFilters.add(name);
    chip.classList.remove("off");
  }
  updateFilterSummary();
  updateStartBtn();
}

function setAllFilters(on) {
  if (on) activeFilters = new Set(allFilters);
  else    activeFilters.clear();
  renderFilterChips();
}

function updateFilterSummary() {
  const on  = activeFilters.size;
  const tot = allFilters.length;
  const el  = document.getElementById("filter-summary");
  if (on === tot) {
    el.innerHTML = `<strong>${on}</strong> of ${tot} filters active (all)`;
  } else if (on === 0) {
    el.innerHTML = `<span style="color:var(--red)">No filters active — no notifications will fire</span>`;
  } else {
    el.innerHTML = `<strong>${on}</strong> of ${tot} active: ${[...activeFilters].join(", ")}`;
  }
}

// ── Step 5: Start ─────────────────────────────────────────────
function updateStartBtn() {
  const ok = selectedSport && selectedGame &&
             Object.keys(selectedPlayers).length > 0 &&
             activeFilters.size > 0;
  document.getElementById("btn-start").disabled = !ok;
  setStep("step-start", !ok);
}

function setStep(id, disabled) {
  document.getElementById(id).classList.toggle("disabled", disabled);
}

function startTracking() {
  socket.emit("start_tracking", {
    sport:      selectedSport,
    game_id:    selectedGame.id,
    game_label: selectedGame.label,
    players:    selectedPlayers,
    filters:    [...activeFilters],
  });
}

function stopTracking() {
  socket.emit("stop_tracking");
  document.getElementById("status-badge").textContent = "Stopped";
  document.getElementById("status-badge").classList.remove("active");
}

function appendLog(text) {
  const panel = document.getElementById("log-panel");
  const line  = document.createElement("div");
  line.textContent = text;
  panel.appendChild(line);
  panel.scrollTop = panel.scrollHeight;
  while (panel.children.length > 80) panel.removeChild(panel.firstChild);
}
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=PORT, debug=False)
