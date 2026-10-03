#!/usr/bin/env python3
"""Serious Games Week matchmaker -- an iGOR-style lobby for a weekly collection of Linux-native serious games.

Anyone can run one: `python3 server.py --port 8080 --db sgweek.db`. Games are told its URL by the player
(SGW_URL, or ~/.config/sgweek/url), so several independent matchmakers can exist.

THE WEEK. Each day of the week has one category of serious games (categories.json). A player may START a game
only in the category of the current day *where the player is* -- the day is computed from the time zone the client
reports -- so everyone rotates through all seven categories during the week. JOINING is open: a game someone
started on their own day stays joinable until it ends. The site lists every category and every game the same
way; nothing is promoted.

API (JSON over HTTP):
  GET    /api/categories                      the week: weekday -> category -> games
  GET    /api/today?tz=Area/City              the category a player in that zone may start games in now
  GET    /api/games[?game=ma]                 live game sessions (any category)
  POST   /api/games                           start: {game, title, port, tz, version?, players?, max_players?,
                                              host?} -> {id, token, expires_in}; 403 if not today's category
  POST   /api/games/<id>/heartbeat            {token, players?} keeps it listed (sessions expire without one)
  DELETE /api/games/<id>                      {token} ends it
  GET    /                                    the week and its live games, for people
The host address defaults to the address the request came from (what other players must reach), as on iGOR.
"""
import argparse
import datetime
import html
import json
import os
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

HERE = os.path.dirname(os.path.abspath(__file__))
EXPIRE_S = 120          # a session not heartbeated for this long disappears
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def load_categories(path):
    with open(path) as f:
        cats = json.load(f)["categories"]
    assert sorted(c["weekday"] for c in cats) == list(range(7)), "categories.json needs one category per weekday"
    return sorted(cats, key=lambda c: c["weekday"])


def local_weekday(tz=None, utc_offset_min=None, now=None):
    """Weekday (0 = Monday) where the player is. tz is an IANA zone; utc_offset_min a fallback."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if tz and ZoneInfo:
        try:
            return now.astimezone(ZoneInfo(tz)).weekday()
        except Exception:
            pass
    if utc_offset_min is not None:
        return (now + datetime.timedelta(minutes=int(utc_offset_min))).weekday()
    raise ValueError("a time zone (tz) or utc_offset_min is required")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("""CREATE TABLE IF NOT EXISTS games (
                id TEXT PRIMARY KEY, token TEXT, game TEXT, category TEXT, title TEXT, host TEXT, port INTEGER,
                version TEXT, players INTEGER, max_players INTEGER, started REAL, seen REAL)""")
            self.db.commit()

    def purge(self):
        with self.lock:
            self.db.execute("DELETE FROM games WHERE seen < ?", (time.time() - EXPIRE_S,))
            self.db.commit()

    def add(self, row):
        with self.lock:
            self.db.execute("INSERT INTO games VALUES (:id,:token,:game,:category,:title,:host,:port,:version,"
                            ":players,:max_players,:started,:seen)", row)
            self.db.commit()

    def touch(self, gid, token, players=None):
        with self.lock:
            cur = self.db.execute("UPDATE games SET seen=?, players=COALESCE(?, players) WHERE id=? AND token=?",
                                  (time.time(), players, gid, token))
            self.db.commit()
            return cur.rowcount == 1

    def remove(self, gid, token):
        with self.lock:
            cur = self.db.execute("DELETE FROM games WHERE id=? AND token=?", (gid, token))
            self.db.commit()
            return cur.rowcount == 1

    def list(self, game=None):
        self.purge()
        q = "SELECT id,game,category,title,host,port,version,players,max_players,started FROM games"
        args = ()
        if game:
            q += " WHERE game=?"
            args = (game,)
        q += " ORDER BY started"          # oldest first: no game is ranked above another
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        keys = ["id", "game", "category", "title", "host", "port", "version", "players", "max_players", "started"]
        return [dict(zip(keys, r)) for r in rows]


def make_handler(store, categories):
    games_to_cat = {g["id"]: c for c in categories for g in c["games"]}

    class H(BaseHTTPRequestHandler):
        server_version = "sgweek/1"

        def log_message(self, fmt, *a):
            if os.environ.get("SGW_LOG"):
                super().log_message(fmt, *a)

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 65536:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/api/categories":
                return self._json(200, {"categories": categories})
            if u.path == "/api/today":
                try:
                    wd = local_weekday(q.get("tz"), q.get("utc_offset_min"))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                c = categories[wd]
                return self._json(200, {"weekday": WEEKDAYS[wd], "category": c["id"], "name": c["name"], "games": c["games"]})
            if u.path == "/api/games":
                return self._json(200, {"games": store.list(q.get("game"))})
            if u.path in ("/", "/index.html"):
                return self._page()
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            try:
                b = self._body()
            except Exception as e:
                return self._json(400, {"error": "bad JSON: %s" % e})
            if u.path == "/api/games":
                game = b.get("game")
                if game not in games_to_cat:
                    return self._json(400, {"error": "unknown game %r" % game})
                try:
                    wd = local_weekday(b.get("tz"), b.get("utc_offset_min"))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                today = categories[wd]
                cat = games_to_cat[game]
                if cat["id"] != today["id"]:
                    return self._json(403, {"error": "today (%s where you are) is %s day: you can start %s games, "
                                            "and join any game already running" %
                                            (WEEKDAYS[wd], today["name"], today["name"]),
                                            "today": today["id"]})
                try:
                    port = int(b.get("port"))
                    assert 0 < port < 65536
                except Exception:
                    return self._json(400, {"error": "port required"})
                gid, token, now = secrets.token_hex(6), secrets.token_hex(16), time.time()
                store.add({"id": gid, "token": token, "game": game, "category": cat["id"],
                           "title": str(b.get("title") or game)[:80], "host": str(b.get("host") or self.client_address[0])[:64],
                           "port": port, "version": str(b.get("version") or "")[:40], "players": int(b.get("players") or 1),
                           "max_players": int(b.get("max_players") or 0), "started": now, "seen": now})
                return self._json(201, {"id": gid, "token": token, "expires_in": EXPIRE_S})
            if u.path.startswith("/api/games/") and u.path.endswith("/heartbeat"):
                gid = u.path.split("/")[3]
                ok = store.touch(gid, b.get("token"), b.get("players"))
                return self._json(200 if ok else 404, {"ok": ok})
            return self._json(404, {"error": "not found"})

        def do_DELETE(self):
            u = urlparse(self.path)
            if u.path.startswith("/api/games/"):
                try:
                    b = self._body()
                except Exception:
                    b = {}
                gid = u.path.split("/")[3]
                ok = store.remove(gid, b.get("token") or parse_qs(u.query).get("token", [None])[0])
                return self._json(200 if ok else 404, {"ok": ok})
            return self._json(404, {"error": "not found"})

        def _page(self):
            live = store.list()
            rows = []
            for c in categories:
                gs = [g for g in live if g["category"] == c["id"]]
                items = "".join("<li><b>%s</b> &mdash; %s, %s:%d, %d player(s)</li>" % (
                    html.escape(g["game"]), html.escape(g["title"]), html.escape(g["host"]), g["port"], g["players"])
                    for g in gs) or "<li class=none>no games running</li>"
                names = ", ".join(html.escape(g["name"]) for g in c["games"])
                rows.append("<section data-weekday=%d><h2>%s &middot; %s</h2><p class=games>%s</p><ul>%s</ul></section>"
                            % (c["weekday"], WEEKDAYS[c["weekday"]], html.escape(c["name"]), names, items))
            page = PAGE.replace("{{SECTIONS}}", "\n".join(rows))
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return H


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Serious Games Week</title><style>
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e3e0d8;--today:#e9f2ea}
@media (prefers-color-scheme:dark){:root{--bg:#191917;--fg:#ecebe6;--muted:#a19f97;--line:#34332f;--today:#203023}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px}
h1{font-size:1.6rem;margin:.2em 0}p.lead{color:var(--muted)}
section{border-top:1px solid var(--line);padding:12px 8px}section.today{background:var(--today);border-radius:8px}
h2{font-size:1.1rem;margin:.2em 0}.games{color:var(--muted);margin:.2em 0}ul{margin:.3em 0 .3em 1.2em;padding:0}
.none{color:var(--muted);list-style:none;margin-left:-1.2em}
</style></head><body><main>
<h1>Serious Games Week</h1>
<p class=lead>One kind of serious game for each day of the week, Linux-native games only. You can start a game in
<b>today's</b> category where you are; you can join any game that is running. Every day gets its turn.</p>
<p id=today></p>
{{SECTIONS}}
</main><script>
const wd=(new Date().getDay()+6)%7;  // Monday = 0, in the viewer's own time zone
document.querySelectorAll('section').forEach(s=>{if(+s.dataset.weekday===wd)s.classList.add('today')});
const t=document.querySelector('section.today h2');
if(t)document.getElementById('today').textContent='Where you are, today is '+t.textContent+'.';
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--db", default=os.path.join(HERE, "sgweek.db"))
    ap.add_argument("--categories", default=os.path.join(HERE, "categories.json"))
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.bind, a.port), make_handler(Store(a.db), load_categories(a.categories)))
    print("Serious Games Week matchmaker on http://%s:%d/" % (a.bind, a.port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
