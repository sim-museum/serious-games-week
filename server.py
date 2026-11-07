#!/usr/bin/env python3
"""Serious Games Week matchmaker -- an iGOR-style lobby for a weekly collection of Linux-native serious games.

Set every game's strength so that you barely succeed: that edge is where you learn.

Anyone can run one: `python3 server.py --port 8080 --db sgw.db`. Games are told its URL by the player
(SGW_URL, or ~/.config/sgw/url), so several independent matchmakers can exist.

THE WEEK. Each day of the week has one category of serious games (categories.json): a SUGGESTION, so that players
who want company know which games others are likely to be playing that day *where they are* (the day is computed
from the time zone the client reports). Any game may be started on any day; the response says whether it is the
day's category. JOINING is open: a game stays joinable until it ends. The site lists every category and every game the same
way; nothing is promoted.

API (JSON over HTTP):
  GET    /api/categories                      the week: weekday -> category -> games
  GET    /api/today?tz=Area/City              the category a player in that zone may start games in now
  GET    /api/games[?game=ma]                 live game sessions (any category)
  POST   /api/games                           start: {game, title, port, tz, version?, players?, max_players?,
                                              host?, name?, build?, note?, password?, ping_port?}
                                              -> {id, token, expires_in, on_day, today}; ping_port: the TCP port
                                              where the host answers GET /ping (players time it: the ping column)
  POST   /api/games/<id>/heartbeat            {token, players?} keeps it listed (sessions expire without one)
  DELETE /api/games/<id>                      {token} ends it
  POST   /api/games/<id>/join                 {name, password?} -> {host, port, member}; a game started with a
                                              password is listed with locked=true and no address until joined
  GET    /api/games/<id>/chat?token=..[&since=<id>]   that game's chat (token = a member's, or the host's own)
  POST   /api/games/<id>/chat                 {token, text} says something to the game's players (1/s per address)
  GET    /                                    the week and its live games, for people
There is no lobby chat (PO 2026-10-18, as on iGOR): chat lives inside a game, among the players who joined it.
Any number of games may run at once, on any day.
The host address defaults to the address the request came from (what other players must reach), as on iGOR.
"""
import argparse
import datetime
import hashlib
import hmac
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
            self.db.execute("""CREATE TABLE IF NOT EXISTS chat (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, name TEXT, text TEXT, addr TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS members (
                gid TEXT, token TEXT PRIMARY KEY, name TEXT, joined REAL)""")
            if "gid" not in [r[1] for r in self.db.execute("PRAGMA table_info(chat)")]:
                # older databases: chat was one lobby for everyone; now every message belongs to one game
                self.db.execute("ALTER TABLE chat ADD COLUMN gid TEXT DEFAULT ''")
                self.db.execute("DELETE FROM chat")
            cols = [r[1] for r in self.db.execute("PRAGMA table_info(games)")]
            if "name" not in cols:      # older databases: add the host player's name
                self.db.execute("ALTER TABLE games ADD COLUMN name TEXT DEFAULT ''")
            if "build" not in cols:     # older databases: add the build (git commit) the host runs
                self.db.execute("ALTER TABLE games ADD COLUMN build TEXT DEFAULT ''")
            if "note" not in cols:      # older databases: add the host's one-line note ("beginners welcome")
                self.db.execute("ALTER TABLE games ADD COLUMN note TEXT DEFAULT ''")
            if "ping_port" not in cols:  # older databases: add the host's ping responder port (sgw announce)
                self.db.execute("ALTER TABLE games ADD COLUMN ping_port INTEGER DEFAULT 0")
            if "lan_host" not in cols:  # older databases: add the address the host reported (C12 NET-WAN-1)
                self.db.execute("ALTER TABLE games ADD COLUMN lan_host TEXT DEFAULT ''")
            if "pwhash" not in cols:    # older databases: add the optional password (salted hash, never the text)
                self.db.execute("ALTER TABLE games ADD COLUMN pwhash TEXT DEFAULT ''")
            self.db.commit()

    def purge(self):
        with self.lock:
            self.db.execute("DELETE FROM games WHERE seen < ?", (time.time() - EXPIRE_S,))
            self._drop_orphans()
            self.db.commit()

    def _drop_orphans(self):
        """A game's members and chat go with it (caller holds the lock)."""
        self.db.execute("DELETE FROM members WHERE gid NOT IN (SELECT id FROM games)")
        self.db.execute("DELETE FROM chat WHERE gid NOT IN (SELECT id FROM games)")

    def add(self, row):
        with self.lock:
            self.db.execute("INSERT INTO games (id,token,game,category,title,host,port,version,players,max_players,"
                            "started,seen,name,build,note,pwhash,ping_port,lan_host) VALUES (:id,:token,:game,:category,:title,:host,:port,:version,"
                            ":players,:max_players,:started,:seen,:name,:build,:note,:pwhash,:ping_port,:lan_host)",
                            dict({"pwhash": "", "ping_port": 0, "lan_host": ""}, **row))
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
            self._drop_orphans()
            self.db.commit()
            return cur.rowcount == 1

    def join(self, gid, password, name, viewer=None):
        """Join a live game: -> ({host, port, member}, None), or (None, "no such game" / "wrong password").
        The member token is the key to the game's chat; a locked game's address is only given here."""
        self.purge()
        with self.lock:
            r = self.db.execute("SELECT host,port,pwhash,ping_port,lan_host FROM games WHERE id=?", (gid,)).fetchone()
            if not r:
                return None, "no such game"
            if r[2] and not pw_ok(password or "", r[2]):
                return None, "wrong password"
            member = secrets.token_hex(16)
            self.db.execute("INSERT INTO members (gid,token,name,joined) VALUES (?,?,?,?)", (gid, member, name, time.time()))
            self.db.commit()
        return {"host": addr_for(viewer, r[0], r[4]), "port": r[1], "member": member, "ping_port": r[3] or 0}, None

    def speaker(self, gid, token):
        """The name a token chats under in game gid: a member's, or the host's (its session token); None if neither."""
        if not token:
            return None
        with self.lock:
            r = self.db.execute("SELECT name FROM members WHERE gid=? AND token=?", (gid, token)).fetchone()
            if r:
                return r[0]
            r = self.db.execute("SELECT name FROM games WHERE id=? AND token=?", (gid, token)).fetchone()
        return (r[0] or "host") if r else None

    CHAT_KEEP = 200        # per game

    def say(self, gid, name, text, addr):
        with self.lock:
            cur = self.db.execute("INSERT INTO chat (ts,name,text,addr,gid) VALUES (?,?,?,?,?)",
                                  (time.time(), name, text, addr, gid))
            self.db.execute("DELETE FROM chat WHERE gid=? AND id <= (SELECT MAX(id) FROM (SELECT id FROM chat WHERE gid=? "
                            "ORDER BY id DESC LIMIT -1 OFFSET ?))", (gid, gid, self.CHAT_KEEP))
            self.db.commit()
            return cur.lastrowid

    def chat(self, gid, since=0, limit=100):
        with self.lock:
            rows = self.db.execute("SELECT id,ts,name,text FROM chat WHERE gid=? AND id > ? ORDER BY id DESC LIMIT ?",
                                   (gid, since, limit)).fetchall()
        return [{"id": r[0], "ts": r[1], "name": r[2], "text": r[3]} for r in reversed(rows)]

    def list(self, game=None, build=None, viewer=None):
        """Sessions of `game`. With `build` (the caller's git commit), only sessions built from that same commit are
        returned, so two different builds are never matched (version skew); the second value counts the others.
        A session that announced no build cannot be checked and counts as different. Without `build` (an older
        client) everything is listed, as before."""
        self.purge()
        q = ("SELECT id,game,category,title,host,port,version,players,max_players,started,name,build,note,"
             "pwhash != '',ping_port,lan_host FROM games")
        args = ()
        if game:
            q += " WHERE game=?"
            args = (game,)
        q += " ORDER BY started"          # oldest first: no game is ranked above another
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        keys = ["id", "game", "category", "title", "host", "port", "version", "players", "max_players", "started", "name",
                "build", "note", "locked", "ping_port", "lan_host"]
        out = [dict(zip(keys, r)) for r in rows]
        hidden = 0
        if build:
            keep = [g for g in out if (g["build"] or "") == build]
            hidden, out = len(out) - len(keep), keep
        now = time.time()
        for g in out:
            g["name"] = g["name"] or ""
            g["note"] = g["note"] or ""
            g["age_s"] = int(now - g["started"])
            g["locked"] = bool(g["locked"])
            g["ping_port"] = g["ping_port"] or 0
            g["host"] = addr_for(viewer, g["host"], g.pop("lan_host") or "")   # C12: same network -> LAN address
            if g["locked"]:              # a passworded game's address is given only to a player who joins (POST .../join)
                g["host"], g["port"], g["ping_port"] = "", 0, 0
        return out, hidden


def announce_addresses(seen, reported):
    """C12 NET-WAN-1 (a): the address joiners are given is the one the matchmaker SEES -- behind a home router that is
    the router's public address -- not the one the host reports, which is its LAN address and useless from outside.
    A host on the matchmaker's own machine is seen as loopback; then the reported address is all there is. The
    reported address is kept too, for players behind the same router (many routers do not loop the public address
    back to the LAN). -> (host, lan_host)"""
    seen, reported = (seen or "").strip(), (reported or "").strip()
    loop = seen.startswith("127.") or seen in ("::1", "localhost", "")
    host = (reported or seen) if loop else seen
    return host[:64], (reported or seen)[:64]


def addr_for(viewer, host, lan_host):
    """The address one viewer should use: the LAN address when the viewer comes from the same public address as the
    host (same home network), else the public one."""
    return lan_host if (viewer and lan_host and viewer == host) else host


def pw_hash(password):
    salt = secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 20000).hex()


def pw_ok(password, stored):
    salt, _, h = stored.partition("$")
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 20000).hex(), h)


def make_handler(store, categories, trusted_proxies=()):
    """trusted_proxies: addresses of reverse proxies (an HTTPS front end on an internet server). For a request from one
    of them the caller is the last X-Forwarded-For entry -- the address the proxy saw; anyone else's X-Forwarded-For
    is ignored, so a player cannot claim another address."""
    trusted = set(trusted_proxies or ())
    games_to_cat = {g["id"]: c for c in categories for g in c["games"]}
    last_said = {}                 # address -> time of its last chat message (1 per second)
    said_lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        server_version = "sgw/1"

        def peer(self):
            """The caller's address: the TCP peer, or behind a trusted reverse proxy the address the proxy saw."""
            ip = self.client_address[0]
            if ip in trusted:
                fwd = [x.strip() for x in (self.headers.get("X-Forwarded-For") or "").split(",") if x.strip()]
                if fwd:
                    return fwd[-1]
            return ip

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
                games, hidden = store.list(q.get("game"), q.get("build"), viewer=self.peer())
                return self._json(200, {"games": games, "hidden_other_builds": hidden})
            if u.path == "/api/chat":
                return self._json(410, {"error": NO_LOBBY})
            if u.path.startswith("/api/games/") and u.path.endswith("/chat"):
                gid = u.path.split("/")[3]
                if store.speaker(gid, q.get("token")) is None:
                    return self._json(403, {"error": "join the game to read its chat"})
                try:
                    since = int(q.get("since") or 0)
                except ValueError:
                    since = 0
                return self._json(200, {"messages": store.chat(gid, since)})
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
                on_day = cat["id"] == today["id"]   # the day's category is a suggestion, not a rule (PO, 2026-10-08)
                try:
                    port = int(b.get("port"))
                    assert 0 < port < 65536
                except Exception:
                    return self._json(400, {"error": "port required"})
                try:
                    ping_port = int(b.get("ping_port") or 0)
                    assert 0 <= ping_port < 65536
                except Exception:
                    return self._json(400, {"error": "bad ping_port"})
                gid, token, now = secrets.token_hex(6), secrets.token_hex(16), time.time()
                store.add({"id": gid, "token": token, "game": game, "category": cat["id"],
                           "title": str(b.get("title") or game)[:80],
                           "host": announce_addresses(self.peer(), str(b.get("host") or ""))[0],
                           "lan_host": announce_addresses(self.peer(), str(b.get("host") or ""))[1],
                           "port": port, "version": str(b.get("version") or "")[:40], "players": int(b.get("players") or 1),
                           "max_players": int(b.get("max_players") or 0), "started": now, "seen": now,
                           "name": str(b.get("name") or "")[:40], "build": str(b.get("build") or "")[:64],
                           "note": " ".join(str(b.get("note") or "").split())[:120],
                           "pwhash": pw_hash(str(b["password"])[:64]) if b.get("password") else "",
                           "ping_port": ping_port})
                return self._json(201, {"id": gid, "token": token, "expires_in": EXPIRE_S,
                                         "on_day": on_day, "today": today["id"]})
            if u.path == "/api/chat":
                return self._json(410, {"error": NO_LOBBY})
            if u.path.startswith("/api/games/") and u.path.endswith("/join"):
                name = " ".join(str(b.get("name") or "").split())[:24]
                if not name:
                    return self._json(400, {"error": "name required"})
                j, err = store.join(u.path.split("/")[3], str(b.get("password") or "")[:64], name, viewer=self.peer())
                if err:
                    return self._json(404 if err == "no such game" else 403, {"error": err})
                return self._json(200, j)
            if u.path.startswith("/api/games/") and u.path.endswith("/chat"):
                gid = u.path.split("/")[3]
                name = store.speaker(gid, b.get("token"))
                if name is None:
                    return self._json(403, {"error": "join the game to chat in it"})
                text = " ".join(str(b.get("text") or "").split())[:300]
                if not text:
                    return self._json(400, {"error": "text required"})
                addr = self.peer()
                with said_lock:
                    now = time.time()
                    if now - last_said.get(addr, 0) < 1.0:
                        return self._json(429, {"error": "one message a second, please"})
                    last_said[addr] = now
                return self._json(201, {"id": store.say(gid, name, text, addr)})
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
            live, _ = store.list(viewer=self.peer())
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


NO_LOBBY = "there is no lobby chat: join a game, then chat inside it (POST /api/games/<id>/join)"
NONE_ROW = "<tr class=none><td colspan=7>no games running</td></tr>"


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
    note = ("<div class=note>" + html.escape(g["note"]) + "</div>") if g.get("note") else ""
    addr = "&#128274; password" if g.get("locked") else "%s:%d" % (html.escape(g["host"]), g["port"])
    return ("<tr><td class=g>%s%s</td><td>%s%s%s</td><td class=addr>%s</td><td class=ping data-id=%s></td><td>%s</td><td class=age>%s</td>"
            "<td><button class=join data-id=%s data-locked=%d data-title=\"%s\">Join</button></td></tr>"
            % (html.escape(game_name(g["game"], categories)), ver, html.escape(g["title"]), who, note,
               addr, html.escape(g["id"]), players, fmt_age(g["age_s"]), html.escape(g["id"]), int(bool(g.get("locked"))),
               html.escape(g["title"])))


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
td.age{color:var(--muted);white-space:nowrap;text-align:right}.ver{color:var(--muted);font-weight:400;font-size:.85em}.note{color:var(--muted);font-size:.9em;margin-top:2px}
tr.none td{color:var(--muted)}
details{margin:16px 0;border:1px solid var(--line);border-radius:8px;padding:8px 12px}
code{font-family:ui-monospace,monospace;background:rgba(127,127,127,.12);padding:1px 4px;border-radius:4px;overflow-wrap:anywhere}
#stamp{color:var(--muted);font-size:.85rem}
#game{border:1px solid var(--accent);border-radius:8px;padding:12px;margin:16px 0}#game[hidden]{display:none}
button.join{font:inherit;font-size:.85rem;padding:1px 10px;border-radius:6px;border:1px solid var(--accent);background:none;
color:var(--accent);cursor:pointer}#where{font-family:ui-monospace,monospace}td.ping{color:var(--muted);white-space:nowrap;text-align:right;font-variant-numeric:tabular-nums}#msgs{max-height:260px;overflow-y:auto;font-size:.95rem;margin:4px 0 8px}
.m{padding:2px 0;overflow-wrap:anywhere}.m .t{color:var(--muted);font-size:.8rem;margin-right:6px}.m b{margin-right:4px}
.say{display:flex;gap:6px;flex-wrap:wrap}.say input{font:inherit;padding:4px 8px;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--fg)}#nm,#pw{width:9em}#tx{flex:1;min-width:12em}.say button{font:inherit;padding:4px 12px;border-radius:6px;
border:1px solid var(--accent);background:var(--accent);color:var(--bg);cursor:pointer}#chaterr{color:#b3261e;font-size:.85rem;margin:4px 0}
footer{margin-top:24px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:.9rem}
</style></head><body><main>
<h1>Serious Games Week</h1>
<p class=lead>One kind of serious game for each day of the week, Linux-native games only. Each day's category is a
suggestion, so you know what others are likely to be playing today where you are. You can start any game on any
day, and join any game that is running.</p>
<div class=bar><span id=today></span><span id=countdown></span><span id=stamp></span></div>
<details><summary>Point your games at this matchmaker</summary>
<p>On each machine, once: <code>sgw url {{URL}}</code> (or <code>export SGW_URL={{URL}}</code>).</p>
<p>There is no lobby chat: join a game to chat with its players. A game with a password shows its address only to
players who join with that password.</p>
<p>MiG Alley and Battle of Britain list a session when you host one and show listed sessions in Join.
FreeFalcon lists you when you go online in Comms without a remote address, and adds listed hosts to the phonebook.
Hosts must accept connections on the game's port.</p></details>
<div id=sections>
{{SECTIONS}}
</div>
<div id=game hidden><h2 id=gtitle></h2>
<form id=joinf class=say autocomplete=off><input id=nm maxlength=24 placeholder="your name" required>
<input id=pw type=password maxlength=64 placeholder="password"><button>Join</button></form>
<div id=inside hidden><p>Connect your game to <span id=where></span>. Chat with this game&rsquo;s players:</p>
<div id=msgs aria-live=polite></div>
<form id=say class=say autocomplete=off><input id=tx maxlength=300 placeholder="say something to this game's players" required>
<button>Send</button></form></div>
<p id=chaterr></p><button id=leave class=join type=button>Close</button></div>
<footer>Set each game&rsquo;s strength so that you barely succeed. That edge is where you learn.</footer>
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
  (g.name?' · '+esc(g.name):'')+(g.note?'<div class=note>'+esc(g.note)+'</div>':'')+'</td><td class=addr>'+(g.locked?'&#128274; password':esc(g.host)+':'+g.port)+
  '</td><td class=ping data-id="'+esc(g.id)+'"></td><td>'+p+'</td><td class=age>'+age(g.age_s)+'</td><td><button class=join data-id="'+esc(g.id)+'" data-locked='+(g.locked?1:0)+' data-title="'+esc(g.title)+'">Join</button></td></tr>';}
async function refresh(){try{
  if(!cats.length)cats=(await (await fetch('/api/categories')).json()).categories;
  const games=(await (await fetch('/api/games')).json()).games;
  document.querySelectorAll('section').forEach(s=>{const mine=games.filter(g=>g.category===s.dataset.cat);
    s.querySelector('tbody').innerHTML=mine.length?mine.map(row).join(''):'<tr class=none><td colspan=7>no games running</td></tr>';});
  pingAll(games);
  mark();document.getElementById('stamp').textContent='updated '+new Date().toLocaleTimeString();
}catch(e){document.getElementById('stamp').textContent='matchmaker unreachable — retrying';}}
// ping: the round trip from THIS browser to each host's ping responder (sgw announce), as iGOR shows it. The first
// request opens the connection; the second, on the same kept-alive connection, is one round trip.
const pings={};
async function pingOne(host,port){const u='http://'+host+':'+port+'/ping';
  try{await fetch(u,{cache:'no-store'});const t=performance.now();await fetch(u,{cache:'no-store'});return Math.round(performance.now()-t);}
  catch(e){return null}}
function showPing(id){const c=document.querySelector('td.ping[data-id="'+id+'"]'),p=pings[id];
  if(c)c.textContent=!p?'':p.pending?'…':p.ms==null?'–':p.ms+' ms';}   // – : the host does not answer pings
function pingAll(games){for(const g of games){if(!g.host||!g.ping_port)continue;const p=pings[g.id];
  if(p&&Date.now()-p.t<30000){showPing(g.id);continue}
  pings[g.id]={ms:p?p.ms:null,t:Date.now(),pending:!p};showPing(g.id);
  pingOne(g.host,g.ping_port).then(ms=>{pings[g.id]={ms,t:Date.now()};showPing(g.id);});}}
let lastId=0,cur=null;   // cur: the game this page has joined {id, member}
function store(k,v){try{if(v===undefined)return localStorage.getItem(k);localStorage.setItem(k,v)}catch(e){return null}}
const $=id=>document.getElementById(id);
function openGame(id,title,locked){cur={id};lastId=0;$('game').hidden=false;$('gtitle').textContent=title;
  $('joinf').hidden=false;$('inside').hidden=true;$('pw').hidden=!locked;$('pw').value='';$('msgs').innerHTML='';
  $('chaterr').textContent='';$('game').scrollIntoView({behavior:'smooth'});}
document.addEventListener('click',ev=>{const b=ev.target.closest('button.join[data-id]');
  if(b)openGame(b.dataset.id,b.dataset.title,b.dataset.locked==='1');});
$('leave').addEventListener('click',()=>{cur=null;$('game').hidden=true;});
async function chat(){if(!cur||!cur.member)return;try{
  const r=await fetch('/api/games/'+cur.id+'/chat?token='+cur.member+'&since='+lastId);
  if(r.status===403){$('chaterr').textContent='this game has ended';cur.member=null;return;}
  const j=await r.json(),box=$('msgs');const atEnd=box.scrollTop+box.clientHeight>=box.scrollHeight-4;
  for(const m of j.messages){lastId=m.id;const d=document.createElement('div');d.className='m';
    d.innerHTML='<span class=t>'+new Date(m.ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})+'</span><b>'+esc(m.name)+'</b>'+esc(m.text);
    box.appendChild(d);}
  if(j.messages.length&&atEnd)box.scrollTop=box.scrollHeight;
}catch(e){}}
$('nm').value=store('sgw-name')||'';
$('joinf').addEventListener('submit',async ev=>{ev.preventDefault();if(!cur)return;store('sgw-name',$('nm').value);$('chaterr').textContent='';
  try{const r=await fetch('/api/games/'+cur.id+'/join',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:$('nm').value,password:$('pw').value})});const j=await r.json();
    if(!r.ok){$('chaterr').textContent=j.error||'not joined';return;}
    cur.member=j.member;$('where').textContent=j.host+':'+j.port;
    if(j.ping_port)pingOne(j.host,j.ping_port).then(ms=>{if(ms!=null)$('where').textContent=j.host+':'+j.port+' (ping '+ms+' ms)';});$('joinf').hidden=true;$('inside').hidden=false;chat();
  }catch(e){$('chaterr').textContent='matchmaker unreachable';}});
$('say').addEventListener('submit',async ev=>{ev.preventDefault();if(!cur||!cur.member)return;$('chaterr').textContent='';
  try{const r=await fetch('/api/games/'+cur.id+'/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({token:cur.member,text:$('tx').value})});
    if(r.ok){$('tx').value='';chat();}else{$('chaterr').textContent=(await r.json()).error||'not sent';}}catch(e){$('chaterr').textContent='matchmaker unreachable';}});
mark();tick();refresh();setInterval(refresh,10000);setInterval(tick,30000);setInterval(chat,3000);
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--db", default=os.path.join(HERE, "sgw.db"))
    ap.add_argument("--categories", default=os.path.join(HERE, "categories.json"))
    ap.add_argument("--trust-proxy", action="append", default=[], metavar="ADDR",
                    help="a reverse proxy whose X-Forwarded-For names the caller (repeatable; e.g. 127.0.0.1 behind "
                         "Caddy or nginx on the same server). Without it the TCP peer is the caller, as on a LAN.")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.bind, a.port), make_handler(Store(a.db), load_categories(a.categories), a.trust_proxy))
    print("Serious Games Week matchmaker on http://%s:%d/%s" % (a.bind, a.port,
          " (callers named by X-Forwarded-For from %s)" % ", ".join(a.trust_proxy) if a.trust_proxy else ""), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
