"""
Sports Tracker Web App  —  Multi-User Edition
==============================================
Each connected user gets their own isolated tracker session.
- Their own tracker thread (sport/game/players)
- Their own ntfy topic (entered in the UI — notifications go to their phone only)
- All socket events scoped to their session ID so no cross-talk between users

Deploy on Railway:
    - No extra config needed. Set PORT env var if required (Railway sets it automatically).

Requirements:
    pip install flask flask-socketio requests MLB-StatsAPI eventlet gunicorn
"""

import os
import threading
import requests
import requests.packages.urllib3 as urllib3
from datetime import date, datetime
from flask import Flask, render_template_string, jsonify, request
from flask_socketio import SocketIO, emit, join_room, leave_room

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

# ─────────────────────────────────────────────────────────────
#  PER-USER SESSION STORE
#  Key: socket session ID (request.sid)
#  Value: {"thread": Thread, "stop": Event, "ntfy_url": str}
# ─────────────────────────────────────────────────────────────
sessions = {}
sessions_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────
#  PER-USER HELPERS  (all scoped to a single sid)
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
    """Send push notification to this user's personal ntfy topic."""
    with sessions_lock:
        session = sessions.get(sid, {})
        ntfy_url = session.get("ntfy_url")
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


def stop_user_session(sid: str):
    """Stop the tracker thread for this user if one is running."""
    with sessions_lock:
        session = sessions.get(sid)
    if session and session.get("stop"):
        session["stop"].set()
        t = session.get("thread")
        if t and t.is_alive():
            t.join(timeout=5)


# ─────────────────────────────────────────────────────────────
#  ESPN HELPERS  (stateless — safe to share across users)
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
#  DATA API ROUTES  (shared, read-only — safe for all users)
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


# ─────────────────────────────────────────────────────────────
#  TRACKER THREADS  (one per user session)
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

                result = play.get("result",{})
                event  = result.get("event","At-bat")
                desc   = result.get("description","") or event
                rbi    = result.get("rbi",0)
                h      = "Top" if about.get("halfInning")=="top" else "Bot"
                inn_n  = about.get("inning","?")
                extra  = f" ({rbi} RBI)" if rbi else ""
                detail = f"{h} {inn_n} | Score: {score}\n{desc}{extra}"
                name   = player_names[batter_id]

                user_log(sid, f"{name}: {event}")
                user_alert(sid, name, event, detail, "MLB")
                user_notify(sid, f"MLB - {name}: {event}", detail, "high")
                new += 1

            user_log(sid, f"{len(plays)} plays checked, {new} new alerts")
        except Exception as e:
            user_log(sid, f"Error: {e}")
        stop_event.wait(POLL_INTERVAL)
    user_log(sid, "MLB tracker stopped.")


def track_espn(sid, sport, league, sport_label, game_id, title, player_ids, player_names, stop_event):
    seen = set()
    user_log(sid, f"{sport_label} tracker started — {', '.join(player_names.values())}")
    while not stop_event.is_set():
        try:
            summary = espn_get(f"{ESPN_WEB}/{sport}/{league}/summary", {"event": game_id})
            if espn_is_final(summary):
                score = espn_score_str(summary)
                user_log(sid, f"Game ended. Final: {score}")
                user_notify(sid, f"{sport_label} Final: {title}", score, "low")
                break

            plays = summary.get("plays",[])
            score = espn_score_str(summary)
            new   = 0
            for play in plays:
                play_id = str(play.get("id","")).strip()
                if not play_id or play_id in seen: continue
                seen.add(play_id)

                pids     = espn_participants(play)
                matching = [p for p in pids if p in player_ids]
                if not matching: continue

                text      = play.get("text","").strip()
                play_type = play.get("type",{}).get("text","Play")
                period    = play.get("period",{}).get("number","?")
                clock     = play.get("clock",{}).get("displayValue","")
                time_str  = f"Q{period} {clock}" if sport_label=="NBA" else f"P{period} {clock}"
                detail    = f"{time_str} | {score}\n{text or play_type}"

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


def track_pga(sid, event_id, event_name, player_ids, player_names, stop_event):
    last_snapshot = {}
    user_log(sid, f"PGA tracker started — {', '.join(player_names.values())}")
    while not stop_event.is_set():
        try:
            summary     = espn_get(f"{ESPN_BASE}/golf/pga/summary", {"event": event_id})
            competitors = []
            for comp in summary.get("competitions",[]):
                competitors.extend(comp.get("competitors",[]))

            new = 0
            for c in competitors:
                athlete = c.get("athlete",{})
                pid     = str(athlete.get("id","")).strip()
                if pid not in player_ids: continue

                name     = player_names[pid]
                score    = str(c.get("score","")).strip()
                status   = str(c.get("status","")).strip()
                ls       = c.get("linescores",[])
                thru     = str(len([l for l in ls if l.get("value") not in ("","-",None)]))
                rounds   = [str(r.get("displayValue","")) for r in c.get("rounds",[])]
                cur_rnd  = rounds[-1] if rounds else "?"
                snapshot = f"{score}|{thru}|{cur_rnd}|{status}"

                if last_snapshot.get(pid) == snapshot: continue
                last_snapshot[pid] = snapshot

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
    join_room(sid)  # each user is in their own room = their sid
    with sessions_lock:
        sessions[sid] = {"thread": None, "stop": None, "ntfy_url": None}
    print(f"[+] User connected: {sid[:8]}")


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    stop_user_session(sid)
    with sessions_lock:
        sessions.pop(sid, None)
    print(f"[-] User disconnected: {sid[:8]}")


@socketio.on("set_ntfy_topic")
def on_set_ntfy(data):
    """User submits their personal ntfy topic from the UI."""
    sid   = request.sid
    topic = data.get("topic","").strip()
    if not topic:
        emit("ntfy_status", {"ok": False, "msg": "Topic cannot be empty."})
        return
    ntfy_url = f"https://ntfy.sh/{topic}"
    with sessions_lock:
        if sid in sessions:
            sessions[sid]["ntfy_url"] = ntfy_url
    # Send a test notification so they know it's working
    try:
        requests.post(ntfy_url,
                      data="Sports Tracker connected! You will receive alerts here.".encode(),
                      headers={"Title": "Sports Tracker Ready",
                               "Tags": "white_check_mark",
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
    players    = data.get("players",{})  # {id: name}

    if not game_id or not players:
        emit("log", {"text": "No game or players selected."})
        return

    # Stop any existing tracker for this user
    stop_user_session(sid)

    stop_event = threading.Event()
    with sessions_lock:
        if sid in sessions:
            sessions[sid]["stop"] = stop_event

    user_notify(sid, f"Tracking {game_label}",
                f"Watching: {', '.join(players.values())}", "low")
    user_log(sid, f"Starting {sport.upper()} tracker for {game_label}")

    if sport == "mlb":
        pid_map = {int(k): v for k, v in players.items()}
        t = threading.Thread(
            target=track_mlb,
            args=(sid, int(game_id), set(pid_map.keys()), pid_map, stop_event),
            daemon=True)

    elif sport in ("nba","nhl"):
        sport_map = {"nba": ("basketball","nba","NBA"), "nhl": ("hockey","nhl","NHL")}
        sp, lg, label = sport_map[sport]
        t = threading.Thread(
            target=track_espn,
            args=(sid, sp, lg, label, game_id, game_label,
                  set(players.keys()), players, stop_event),
            daemon=True)

    elif sport == "pga":
        t = threading.Thread(
            target=track_pga,
            args=(sid, game_id, game_label,
                  set(players.keys()), players, stop_event),
            daemon=True)

    else:
        emit("log", {"text": f"Unknown sport: {sport}"})
        return

    with sessions_lock:
        if sid in sessions:
            sessions[sid]["thread"] = t
    t.start()
    emit("tracking_started", {"game": game_label, "players": list(players.values())})


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
    --bg:      #0a0e17;
    --surface: #111827;
    --card:    #1a2235;
    --border:  #2a3a55;
    --accent:  #f59e0b;
    --accent2: #3b82f6;
    --green:   #10b981;
    --red:     #ef4444;
    --text:    #e2e8f0;
    --muted:   #64748b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Barlow', sans-serif; min-height: 100vh; padding-bottom: 2rem; }

  header {
    background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%);
    border-bottom: 2px solid var(--accent);
    padding: 1.2rem 1.5rem;
    display: flex; align-items: center; gap: 1rem;
  }
  header h1 { font-family: 'Barlow Condensed', sans-serif; font-weight: 900; font-size: 1.6rem; letter-spacing: 0.05em; text-transform: uppercase; color: #fff; }
  header h1 span { color: var(--accent); }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--muted); flex-shrink: 0; transition: background 0.3s; }
  .dot.live { background: var(--green); box-shadow: 0 0 8px var(--green); animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1}50%{opacity:0.4} }

  .container { max-width: 680px; margin: 0 auto; padding: 1.5rem 1rem; }

  .step { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1.2rem; margin-bottom: 1rem; transition: opacity 0.3s; }
  .step.disabled { opacity: 0.35; pointer-events: none; }
  .step-label { font-family: 'Barlow Condensed', sans-serif; font-size: 0.75rem; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; color: var(--accent); margin-bottom: 0.7rem; }

  .btn-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 0.5rem; }
  .btn-grid.tight { grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); }

  button { background: var(--card); border: 1.5px solid var(--border); color: var(--text); border-radius: 8px; padding: 0.65rem 0.8rem; font-family: 'Barlow', sans-serif; font-size: 0.88rem; font-weight: 500; cursor: pointer; text-align: left; transition: border-color 0.15s, background 0.15s, transform 0.1s; line-height: 1.3; }
  button:active { transform: scale(0.97); }
  button:hover  { border-color: var(--accent2); background: #1f3050; }
  button.selected { border-color: var(--accent); background: #2a1f00; color: var(--accent); }
  button.player-btn.selected { border-color: var(--green); background: #0d2a1f; color: var(--green); }
  button.game-btn .badge { display: block; font-size: 0.72rem; color: var(--muted); margin-top: 2px; }
  button.game-btn.live-game { border-color: #16a34a; }
  button.game-btn.live-game .badge { color: var(--green); }

  .action-row { display: flex; gap: 0.6rem; margin-top: 0.5rem; }
  .btn-start { flex: 1; background: var(--accent); border-color: var(--accent); color: #000; font-weight: 700; font-family: 'Barlow Condensed', sans-serif; font-size: 1rem; letter-spacing: 0.05em; text-transform: uppercase; text-align: center; padding: 0.8rem; border-radius: 8px; }
  .btn-start:hover { background: #d97706; border-color: #d97706; color: #000; }
  .btn-start:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-stop { background: transparent; border-color: var(--red); color: var(--red); font-weight: 600; text-align: center; padding: 0.8rem 1.2rem; border-radius: 8px; }
  .btn-stop:hover { background: #2a1010; }

  .selection-summary { font-size: 0.82rem; color: var(--muted); margin-top: 0.6rem; min-height: 1.2em; }
  .selection-summary strong { color: var(--text); }

  /* ntfy input */
  .ntfy-row { display: flex; gap: 0.5rem; align-items: stretch; }
  .ntfy-row input { flex: 1; background: var(--card); border: 1.5px solid var(--border); border-radius: 8px; padding: 0.6rem 0.8rem; color: var(--text); font-family: 'Barlow', sans-serif; font-size: 0.9rem; outline: none; }
  .ntfy-row input:focus { border-color: var(--accent2); }
  .ntfy-row input::placeholder { color: var(--muted); }
  .btn-ntfy { background: var(--accent2); border-color: var(--accent2); color: #fff; font-weight: 600; font-size: 0.85rem; padding: 0.6rem 1rem; border-radius: 8px; text-align: center; white-space: nowrap; }
  .btn-ntfy:hover { background: #2563eb; border-color: #2563eb; }
  .ntfy-status { font-size: 0.8rem; margin-top: 0.5rem; min-height: 1.2em; }
  .ntfy-status.ok  { color: var(--green); }
  .ntfy-status.err { color: var(--red); }

  .search-wrap { position: relative; margin-bottom: 0.6rem; }
  .search-wrap input { width: 100%; background: var(--card); border: 1.5px solid var(--border); border-radius: 8px; padding: 0.6rem 0.8rem; color: var(--text); font-family: 'Barlow', sans-serif; font-size: 0.9rem; outline: none; }
  .search-wrap input:focus { border-color: var(--accent2); }
  .search-wrap input::placeholder { color: var(--muted); }

  .feed-panel { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; margin-bottom: 1rem; }
  .feed-header { background: var(--card); padding: 0.7rem 1rem; font-family: 'Barlow Condensed', sans-serif; font-size: 0.8rem; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted); display: flex; justify-content: space-between; align-items: center; }
  #status-badge { font-size: 0.72rem; background: var(--bg); border-radius: 4px; padding: 2px 7px; color: var(--muted); }
  #status-badge.active { color: var(--green); border: 1px solid var(--green); }

  #alerts-list { padding: 0.5rem; }
  .alert-card { background: var(--card); border-left: 3px solid var(--accent); border-radius: 6px; padding: 0.7rem 0.9rem; margin-bottom: 0.4rem; animation: slideIn 0.3s ease; }
  .alert-card.nba { border-color: #f97316; }
  .alert-card.nhl { border-color: #a78bfa; }
  .alert-card.mlb { border-color: var(--accent); }
  .alert-card.pga { border-color: var(--green); }
  @keyframes slideIn { from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none} }
  .alert-player { font-family: 'Barlow Condensed', sans-serif; font-size: 1rem; font-weight: 700; color: #fff; }
  .alert-event  { font-size: 0.78rem; color: var(--accent); font-weight: 600; margin: 1px 0; }
  .alert-detail { font-size: 0.8rem; color: var(--muted); white-space: pre-line; }
  .alert-time   { font-size: 0.7rem; color: var(--border); float: right; }

  #log-panel { background: #070b12; border: 1px solid var(--border); border-radius: 8px; padding: 0.6rem 0.8rem; font-family: 'Barlow Condensed', monospace; font-size: 0.78rem; color: var(--muted); max-height: 130px; overflow-y: auto; }
  #log-panel div { margin-bottom: 1px; }

  .empty-state { text-align: center; padding: 2rem 1rem; color: var(--muted); font-size: 0.88rem; }
  .loader { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.7s linear infinite; vertical-align: middle; margin-left: 6px; }
  @keyframes spin { to{transform:rotate(360deg)} }
</style>
</head>
<body>
<header>
  <div class="dot" id="conn-dot"></div>
  <h1>Sports <span>Tracker</span></h1>
</header>
<div class="container">

  <!-- STEP 0: NTFY TOPIC -->
  <div class="step" id="step-ntfy">
    <div class="step-label">Step 1 — Connect Your iPhone Notifications</div>
    <p style="font-size:0.83rem;color:var(--muted);margin-bottom:0.7rem;">
      Install the free <strong style="color:var(--text)">ntfy</strong> app on your iPhone, subscribe to any topic name you choose, then enter it below.
    </p>
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

  <!-- STEP 4: START -->
  <div class="step disabled" id="step-start">
    <div class="step-label">Step 5 — Start Tracking</div>
    <div class="action-row">
      <button class="btn-start" id="btn-start" onclick="startTracking()" disabled>Start Tracking</button>
      <button class="btn-stop" onclick="stopTracking()">Stop</button>
    </div>
  </div>

  <!-- LIVE FEED -->
  <div class="feed-panel" id="feed-panel" style="display:none">
    <div class="feed-header">
      Live Alerts
      <span id="status-badge">Idle</span>
    </div>
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
let ntfyConnected  = false;
let selectedSport  = null;
let selectedGame   = null;
let selectedPlayers = {};
let allPlayers     = {};

// ── Connection ───────────────────────────────────────────────
socket.on("connect",    () => document.getElementById("conn-dot").classList.add("live"));
socket.on("disconnect", () => document.getElementById("conn-dot").classList.remove("live"));

// ── Live events — scoped to this user only by the server ─────
socket.on("log", data => appendLog(data.text));

socket.on("alert", data => {
  document.getElementById("feed-panel").style.display = "block";
  const list  = document.getElementById("alerts-list");
  const empty = list.querySelector(".empty-state");
  if (empty) empty.remove();
  const card = document.createElement("div");
  card.className = `alert-card ${data.sport.toLowerCase()}`;
  card.innerHTML = `
    <span class="alert-time">${data.time}</span>
    <div class="alert-player">${data.player}</div>
    <div class="alert-event">${data.event}</div>
    <div class="alert-detail">${data.detail}</div>`;
  list.insertBefore(card, list.firstChild);
  while (list.children.length > 30) list.removeChild(list.lastChild);
});

socket.on("tracking_started", data => {
  document.getElementById("status-badge").textContent = "LIVE";
  document.getElementById("status-badge").classList.add("active");
  document.getElementById("feed-panel").style.display = "block";
  document.getElementById("log-panel-wrap").style.display = "block";
  appendLog(`Tracking: ${data.game} | ${data.players.join(", ")}`);
});

socket.on("ntfy_status", data => {
  const el = document.getElementById("ntfy-status");
  el.textContent = data.msg;
  el.className   = "ntfy-status " + (data.ok ? "ok" : "err");
  if (data.ok) {
    ntfyConnected = true;
    setStep("step-sport", false);  // unlock sport selection
  }
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
  selectedSport   = sport;
  selectedGame    = null;
  selectedPlayers = {};
  allPlayers      = {};

  document.querySelectorAll("#step-sport button").forEach(b => b.classList.remove("selected"));
  btn.classList.add("selected");

  setStep("step-game", false);
  setStep("step-players", true);
  setStep("step-start", true);
  document.getElementById("games-list").innerHTML = '<div class="empty-state"><span class="loader"></span> Loading...</div>';
  document.getElementById("players-list").innerHTML = '<div class="empty-state">Select a game first</div>';
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
        const btn = document.createElement("button");
        btn.className = "game-btn" + (g.live ? " live-game" : "");
        btn.innerHTML = `${g.label}<span class="badge">${g.badge}</span>`;
        btn.onclick   = () => selectGame(g, btn);
        list.appendChild(btn);
      });
    })
    .catch(() => document.getElementById("games-list").innerHTML = '<div class="empty-state">Failed to load games</div>');
}

// ── Step 2: Game ──────────────────────────────────────────────
function selectGame(game, btn) {
  selectedGame    = game;
  selectedPlayers = {};
  allPlayers      = {};
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
    })
    .catch(() => {
      document.getElementById("players-list").innerHTML = '<div class="empty-state">Failed to load players</div>';
      setStep("step-players", false);
    });
}

function renderPlayers(players) {
  const list    = document.getElementById("players-list");
  const q       = document.getElementById("player-search").value.toLowerCase();
  list.innerHTML = "";
  const entries = Object.entries(players)
    .filter(([id, p]) => p.name.toLowerCase().includes(q))
    .sort((a, b) => a[1].name.localeCompare(b[1].name));
  if (!entries.length) { list.innerHTML = '<div class="empty-state">No players found</div>'; return; }
  entries.forEach(([id, p]) => {
    const btn = document.createElement("button");
    btn.className   = "player-btn" + (selectedPlayers[id] ? " selected" : "");
    btn.textContent = p.name + (p.pos ? ` (${p.pos})` : "");
    btn.dataset.id  = id;
    btn.onclick     = () => togglePlayer(id, p.name, btn);
    list.appendChild(btn);
  });
}

function filterPlayers() { renderPlayers(allPlayers); }

// ── Step 3: Players ───────────────────────────────────────────
function togglePlayer(id, name, btn) {
  if (selectedPlayers[id]) { delete selectedPlayers[id]; btn.classList.remove("selected"); }
  else                      { selectedPlayers[id] = name;  btn.classList.add("selected"); }
  const count = Object.keys(selectedPlayers).length;
  document.getElementById("player-summary").innerHTML =
    count ? `<strong>${count}</strong> player${count>1?"s":""} selected: ${Object.values(selectedPlayers).join(", ")}` : "";
  updateStartBtn();
}

// ── Step 4: Start ─────────────────────────────────────────────
function updateStartBtn() {
  const ok = selectedSport && selectedGame && Object.keys(selectedPlayers).length > 0;
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
  });
}

function stopTracking() {
  socket.emit("stop_tracking");
  document.getElementById("status-badge").textContent = "Stopped";
  document.getElementById("status-badge").classList.remove("active");
}

// ── Log ───────────────────────────────────────────────────────
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
