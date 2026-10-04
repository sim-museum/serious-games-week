#!/usr/bin/env python3
"""Serious Games Week matchmaker -- an iGOR-style lobby for a weekly collection of Linux-native serious games.

"sgweek" is pronounced "squeak". The name is a reminder to set every game's strength so that you barely succeed, so
that you only just squeak by.

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
            cols = [r[1] for r in self.db.execute("PRAGMA table_info(games)")]
            if "name" not in cols:      # older databases: add the host player's name
                self.db.execute("ALTER TABLE games ADD COLUMN name TEXT DEFAULT ''")
            self.db.commit()

    def purge(self):
        with self.lock:
            self.db.execute("DELETE FROM games WHERE seen < ?", (time.time() - EXPIRE_S,))
            self.db.commit()

    def add(self, row):
        with self.lock:
            self.db.execute("INSERT INTO games (id,token,game,category,title,host,port,version,players,max_players,"
                            "started,seen,name) VALUES (:id,:token,:game,:category,:title,:host,:port,:version,"
                            ":players,:max_players,:started,:seen,:name)", row)
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
        q = "SELECT id,game,category,title,host,port,version,players,max_players,started,name FROM games"
        args = ()
        if game:
            q += " WHERE game=?"
            args = (game,)
        q += " ORDER BY started"          # oldest first: no game is ranked above another
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        keys = ["id", "game", "category", "title", "host", "port", "version", "players", "max_players", "started", "name"]
        out = [dict(zip(keys, r)) for r in rows]
        now = time.time()
        for g in out:
            g["name"] = g["name"] or ""
            g["age_s"] = int(now - g["started"])
        return out


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
                           "max_players": int(b.get("max_players") or 0), "started": now, "seen": now,
                           "name": str(b.get("name") or "")[:40]})
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
            host_hdr = self.headers.get("Host") or ("%s:%d" % self.server.server_address[:2])
            rows = []
            for c in categories:
                gs = [g for g in live if g["category"] == c["id"]]
                rows.append("<section data-weekday=%d data-cat=%s><h2>%s &middot; %s</h2><p class=games>%s</p>"
                            "<table><tbody>%s</tbody></table></section>"
                            % (c["weekday"], html.escape(c["id"]), WEEKDAYS[c["weekday"]], html.escape(c["name"]),
                               ", ".join(html.escape(g["name"]) for g in c["games"]),
                               "".join(row_html(g, categories) for g in gs) or NONE_ROW))
            page = (PAGE.replace("{{SECTIONS}}", "\n".join(rows))
                        .replace("{{URL}}", html.escape("http://" + host_hdr))
                        .replace("{{CATS}}", json.dumps(categories).replace("</", "<\\/")))
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return H


NONE_ROW = "<tr class=none><td colspan=5>no games running</td></tr>"


def game_name(gid, categories):
    for c in categories:
        for g in c["games"]:
            if g["id"] == gid:
                return g["name"]
    return gid


def fmt_age(sec):
    m = sec // 60
    return "%dm" % m if m < 60 else "%dh%02dm" % (m // 60, m % 60)


def row_html(g, categories):
    players = "%d" % g["players"] + ("/%d" % g["max_players"] if g["max_players"] else "")
    who = (" &middot; " + html.escape(g["name"])) if g["name"] else ""
    ver = (" <span class=ver>v" + html.escape(g["version"]) + "</span>") if g["version"] else ""
    return ("<tr><td class=g>%s%s</td><td>%s%s</td><td class=addr>%s:%d</td><td>%s</td><td class=age>%s</td></tr>"
            % (html.escape(game_name(g["game"], categories)), ver, html.escape(g["title"]), who,
               html.escape(g["host"]), g["port"], players, fmt_age(g["age_s"])))


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Serious Games Week</title><style>
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e3e0d8;--today:#e9f2ea;--accent:#2f6b3a}
@media (prefers-color-scheme:dark){:root{--bg:#191917;--fg:#ecebe6;--muted:#a19f97;--line:#34332f;--today:#203023;--accent:#8fd19a}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:820px;margin:0 auto;padding:24px 16px}
h1{font-size:1.6rem;margin:.2em 0}p.lead{color:var(--muted)}
.bar{display:flex;flex-wrap:wrap;gap:8px 24px;align-items:baseline;margin:8px 0 16px}
#today{font-weight:600}#countdown{color:var(--accent);font-variant-numeric:tabular-nums}
section{border-top:1px solid var(--line);padding:12px 8px}section.today{background:var(--today);border-radius:8px}
h2{font-size:1.1rem;margin:.2em 0}.games{color:var(--muted);margin:.2em 0 .4em}
table{width:100%;border-collapse:collapse;font-size:.95rem}td{padding:3px 6px;vertical-align:top}
td.g{font-weight:600;white-space:nowrap}td.addr{font-family:ui-monospace,monospace;white-space:nowrap}
td.age{color:var(--muted);white-space:nowrap;text-align:right}.ver{color:var(--muted);font-weight:400;font-size:.85em}
tr.none td{color:var(--muted)}
details{margin:16px 0;border:1px solid var(--line);border-radius:8px;padding:8px 12px}
code{font-family:ui-monospace,monospace;background:rgba(127,127,127,.12);padding:1px 4px;border-radius:4px;overflow-wrap:anywhere}
#stamp{color:var(--muted);font-size:.85rem}
footer{margin-top:24px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:.9rem}
</style></head><body><main>
<h1>Serious Games Week</h1>
<p class=lead>One kind of serious game for each day of the week, Linux-native games only. You can start a game in
<b>today's</b> category where you are; you can join any game that is running. Every day gets its turn.</p>
<div class=bar><span id=today></span><span id=countdown></span><span id=stamp></span></div>
<details><summary>Point your games at this matchmaker</summary>
<p>On each machine, once: <code>sgw url {{URL}}</code> (or <code>export SGW_URL={{URL}}</code>).</p>
<p>MiG Alley and Battle of Britain list a session when you host one and show listed sessions in Join.
FreeFalcon lists you when you go online in Comms without a remote address, and adds listed hosts to the phonebook.
Hosts must accept connections on the game's port. A game outside today's category is not listed, and the game
says why.</p></details>
<div id=sections>
{{SECTIONS}}
</div>
<footer><b>sgweek</b> is pronounced &ldquo;squeak&rdquo;: set each game&rsquo;s strength so you barely succeed &mdash;
so you only just <i>squeak by</i>. That edge is where you learn.</footer>
</main><script>
const WD=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];
const wd=(new Date().getDay()+6)%7;  // Monday = 0, in the viewer's own time zone
let cats={{CATS}};   // embedded, so the day and countdown show before the first refresh
function esc(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function age(s){const m=Math.floor(s/60);return m<60?m+"m":Math.floor(m/60)+"h"+String(m%60).padStart(2,"0")+"m"}
function gname(id){for(const c of cats)for(const g of c.games)if(g.id===id)return g.name;return id}
function mark(){document.querySelectorAll('section').forEach(s=>s.classList.toggle('today',+s.dataset.weekday===wd));
  const c=cats[wd]; if(c)document.getElementById('today').textContent='Today where you are: '+WD[wd]+' · '+c.name;}
function tick(){const n=new Date(),m=new Date(n);m.setHours(24,0,0,0);const s=Math.max(0,Math.floor((m-n)/1000));
  document.getElementById('countdown').textContent='ends in '+Math.floor(s/3600)+'h '+String(Math.floor(s%3600/60)).padStart(2,'0')+'m';
  if(((n.getDay()+6)%7)!==wd)location.reload();}
function row(g){const p=g.players+(g.max_players?'/'+g.max_players:'');
  return '<tr><td class=g>'+esc(gname(g.game))+(g.version?' <span class=ver>v'+esc(g.version)+'</span>':'')+'</td><td>'+esc(g.title)+
  (g.name?' · '+esc(g.name):'')+'</td><td class=addr>'+esc(g.host)+':'+g.port+'</td><td>'+p+'</td><td class=age>'+age(g.age_s)+'</td></tr>';}
async function refresh(){try{
  if(!cats.length)cats=(await (await fetch('/api/categories')).json()).categories;
  const games=(await (await fetch('/api/games')).json()).games;
  document.querySelectorAll('section').forEach(s=>{const mine=games.filter(g=>g.category===s.dataset.cat);
    s.querySelector('tbody').innerHTML=mine.length?mine.map(row).join(''):'<tr class=none><td colspan=5>no games running</td></tr>';});
  mark();document.getElementById('stamp').textContent='updated '+new Date().toLocaleTimeString();
}catch(e){document.getElementById('stamp').textContent='matchmaker unreachable — retrying';}}
mark();tick();refresh();setInterval(refresh,10000);setInterval(tick,30000);
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
