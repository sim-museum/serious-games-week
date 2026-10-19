#!/usr/bin/env python3
"""sgw -- the Serious Games Week matchmaker client the games call (stdlib only).

The matchmaker URL is the player's choice: $SGW_URL, else the first line of ~/.config/sgw/url.
  sgw url                                      print the configured matchmaker
  sgw url https://games.example.org            choose the matchmaker (writes ~/.config/sgw/url)
  sgw today                                    today's category where you are (and its games)
  sgw list --game ma [--json]                  joinable sessions: one "host port players title" line each
                                               (passworded games: named on stderr; --json --locked lists them); --json adds
                                               ping_ms, the round trip to each host from here (--no-ping: skip)
  sgw announce --game ma --port 47734 --title "Spring Offensive" [--name N --players N --max N --version V --build B --note T]
                                               list this host's game and keep it listed (heartbeat) until
                                               killed (SIGTERM/SIGINT) or stdin closes -- the game spawns it when
                                               it starts hosting and kills it when the session ends. Any game,
                                               any day (today's category is a suggestion).
                                               --password P (or $SGW_PASSWORD): listed with a lock and no address;
                                               players join with the password. While listed it answers pings on a
                                               TCP port (--ping-port, default any free one) that players time.
  sgw join --id ID [--password P --name N]     join a listed game: prints "host port member-token"
  sgw chat --id ID --token T [--follow]        that game's chat (there is no lobby chat; T from join)
  sgw say --id ID --token T "text"             say something to that game's players
Exit codes: 0 ok, 2 no matchmaker configured, 3 refused by the matchmaker, 4 network error.
"""
import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "1.0.0"


CONFIG = os.path.expanduser("~/.config/sgw/url")
LEGACY_CONFIG = os.path.expanduser("~/.config/sgweek/url")   # read-only fallback for setups made before 2026-10-05


def matchmaker_url():
    u = os.environ.get("SGW_URL")
    if not u:
        for p in (CONFIG, LEGACY_CONFIG):
            if os.path.exists(p):
                with open(p) as f:
                    u = f.readline().strip()
                break
    return u.rstrip("/") if u else None


def local_tz():
    tz = os.environ.get("TZ")
    if tz and "/" in tz:
        return tz.lstrip(":")
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return None


def utc_offset_min():
    return int(-(time.altzone if time.localtime().tm_isdst > 0 else time.timezone) / 60)


def call(base, method, path, body=None, timeout=10):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": "sgw/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {"error": str(e)}


def ping_responder(port=0):
    """While a game is listed, answer GET /ping (any origin) on a TCP port, so a player's page or `sgw list` can time
    the round trip to this host, as iGOR shows a ping per game. -> the server, or None if the port cannot be bound."""
    class Ping(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"          # keep-alive: a browser's second request is one clean round trip

        def do_GET(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass
    try:
        srv = ThreadingHTTPServer(("", port), Ping)
    except OSError:
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def ping_ms(host, port, timeout=1.0, tries=3):
    """Round trip from here to a host's ping responder in ms: the best of `tries` TCP connects (a connect is one
    SYN / SYN-ACK round trip); None if it does not answer."""
    best = None
    for _ in range(tries):
        t = time.perf_counter()
        try:
            socket.create_connection((host, port), timeout).close()
        except OSError:
            break
        d = (time.perf_counter() - t) * 1000.0
        best = d if best is None else min(best, d)
    return None if best is None else round(best, 1)


def add_pings(games):
    """ping_ms on every game whose address and ping port are known (in parallel: one slow host does not stall the list)."""
    ts = []
    for g in games:
        g["ping_ms"] = None
        if g.get("host") and g.get("ping_port"):
            t = threading.Thread(target=lambda g=g: g.__setitem__("ping_ms", ping_ms(g["host"], g["ping_port"])), daemon=True)
            t.start(); ts.append(t)
    for t in ts:
        t.join(4)
    return games


def where():
    tz = local_tz()
    return {"tz": tz} if tz else {"utc_offset_min": utc_offset_min()}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sgw", description="Serious Games Week matchmaker client")
    ap.add_argument("--version", action="version", version="sgw " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("url"); u.add_argument("set", nargs="?", help="the matchmaker's URL, to make it this player's")
    sub.add_parser("today")
    l = sub.add_parser("list"); l.add_argument("--game", required=True); l.add_argument("--json", action="store_true")
    # --build: the git commit this game was built from ($SGW_BUILD). Only sessions built from the same commit are
    # listed, so two different builds never meet (version skew); the matchmaker reports how many it left out.
    l.add_argument("--build", default=os.environ.get("SGW_BUILD", ""))
    l.add_argument("--no-ping", dest="ping", action="store_false", help="--json without timing each host (ping_ms)")
    l.add_argument("--locked", action="store_true", help="--json also lists passworded games (locked, no address)")
    # There is no lobby chat: `join` a game (its password if it has one) for its address and a member token, then
    # `chat`/`say` inside that game with the token.
    me = os.environ.get("SGW_NAME") or os.environ.get("USER", "")
    jn = sub.add_parser("join"); jn.add_argument("--id", required=True); jn.add_argument("--name", default=me)
    jn.add_argument("--password", default=os.environ.get("SGW_PASSWORD", ""))
    c = sub.add_parser("chat"); c.add_argument("--id", required=True); c.add_argument("--token", required=True)
    c.add_argument("--follow", action="store_true")
    y = sub.add_parser("say"); y.add_argument("--id", required=True); y.add_argument("--token", required=True)
    y.add_argument("text")
    a = sub.add_parser("announce")
    a.add_argument("--game", required=True); a.add_argument("--port", type=int, required=True)
    a.add_argument("--title", default=""); a.add_argument("--players", type=int, default=1)
    a.add_argument("--max", type=int, default=0); a.add_argument("--version", default="")
    a.add_argument("--build", default=os.environ.get("SGW_BUILD", ""), help="git commit of this build ($SGW_BUILD)")
    a.add_argument("--host", default=None, help="address players should use (default: as the matchmaker sees you)")
    a.add_argument("--name", default=os.environ.get("SGW_NAME") or os.environ.get("USER", ""),
                   help="your name as other players see it (default: $SGW_NAME, else your login)")
    a.add_argument("--note", default="", help="one line other players see, e.g. \"beginners welcome\"")
    a.add_argument("--password", default=os.environ.get("SGW_PASSWORD", ""),
                   help="players must give it to see the address and chat ($SGW_PASSWORD); empty = open game")
    a.add_argument("--ping-port", type=int, default=int(os.environ.get("SGW_PING_PORT") or 0),
                   help="TCP port for the ping responder ($SGW_PING_PORT; default: any free port)")
    a.add_argument("--every", type=float, default=30.0)
    args = ap.parse_args(argv)

    if args.cmd == "url" and args.set:
        if not args.set.startswith(("http://", "https://")):
            print("sgw: a matchmaker URL starts with http:// or https://", file=sys.stderr)
            return 2
        p = CONFIG
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(args.set.rstrip("/") + "\n")
        print("sgw: matchmaker set to %s (your games read it when they start)" % args.set.rstrip("/"))
        if os.environ.get("SGW_URL"):
            print("sgw: note: SGW_URL is set in this shell and overrides it", file=sys.stderr)
        return 0
    base = matchmaker_url()
    if args.cmd == "url":
        print(base or "")
        return 0 if base else 2
    if not base:
        print("sgw: no matchmaker configured (set SGW_URL or write it to ~/.config/sgw/url)", file=sys.stderr)
        return 2
    try:
        if args.cmd == "today":
            q = "&".join("%s=%s" % kv for kv in where().items())
            code, j = call(base, "GET", "/api/today?" + q)
            if code != 200:
                print("sgw: %s" % j.get("error"), file=sys.stderr); return 3
            print("%s: %s (%s)" % (j["weekday"], j["name"], ", ".join(g["name"] for g in j["games"])))
            return 0
        if args.cmd == "list":
            code, j = call(base, "GET", "/api/games?game=" + args.game + ("&build=" + args.build if args.build else ""))
            if code != 200:
                print("sgw: %s" % j.get("error"), file=sys.stderr); return 3
            if j.get("hidden_other_builds"):
                print("sgw: %d %s game(s) not shown: built from a different commit than yours (%s)"
                      % (j["hidden_other_builds"], args.game, args.build), file=sys.stderr)
            # a passworded game has no address until joined, so the games' own lists (text, and --json without
            # --locked) leave it out: an older game would otherwise try to connect to ":0"
            locked = [g for g in j["games"] if g.get("locked")]
            if locked and not (args.json and args.locked):
                print("sgw: %d %s game(s) need a password: sgw join --id <id> --password <pw> (%s)"
                      % (len(locked), args.game, ", ".join(g["id"] for g in locked)), file=sys.stderr)
            games = j["games"] if (args.json and args.locked) else [g for g in j["games"] if not g.get("locked")]
            if args.json:
                print(json.dumps(add_pings(games) if args.ping else games))
            else:
                for g in games:
                    print("%s %d %d %s" % (g["host"], g["port"], g["players"], g["title"].replace("\n", " ")))
            return 0
        if args.cmd == "join":
            code, j = call(base, "POST", "/api/games/%s/join" % args.id, {"name": args.name, "password": args.password})
            if code != 200:
                print("sgw: %s" % j.get("error"), file=sys.stderr); return 3
            print("%s %d %s" % (j["host"], j["port"], j["member"]))      # host port member-token
            return 0
        if args.cmd in ("chat", "say"):
            path = "/api/games/%s/chat" % args.id
            if args.cmd == "say":
                code, j = call(base, "POST", path, {"token": args.token, "text": args.text})
                if code != 201:
                    print("sgw: %s" % j.get("error"), file=sys.stderr); return 3
                return 0
            last = 0
            while True:
                code, j = call(base, "GET", "%s?token=%s&since=%d" % (path, args.token, last))
                if code != 200:
                    print("sgw: %s" % j.get("error"), file=sys.stderr); return 3
                for m in j.get("messages", []):
                    last = m["id"]
                    print("%s  %s: %s" % (time.strftime("%H:%M", time.localtime(m["ts"])), m["name"], m["text"]), flush=True)
                if not args.follow:
                    return 0
                time.sleep(3)
        # announce
        body = dict(where(), game=args.game, port=args.port, title=args.title, players=args.players,
                    max_players=args.max, version=args.version, name=args.name, build=args.build,
                    note=args.note, password=args.password)
        if args.host:
            body["host"] = args.host
        ping = ping_responder(args.ping_port)
        if ping:
            body["ping_port"] = ping.server_address[1]
        else:
            print("sgw: ping port %d busy: listed without a ping" % args.ping_port, file=sys.stderr)
        code, j = call(base, "POST", "/api/games", body)
        if code != 201:
            print("sgw: %s" % j.get("error", "refused (%d)" % code), file=sys.stderr)
            return 3
        gid, token = j["id"], j["token"]
        print("sgw: listed as %s on %s" % (gid, base), flush=True)
        stop = threading.Event()
        for s in (signal.SIGTERM, signal.SIGINT):
            signal.signal(s, lambda *_: stop.set())
        if not sys.stdin.isatty():
            threading.Thread(target=lambda: (sys.stdin.read(), stop.set()), daemon=True).start()
        while not stop.wait(args.every):
            call(base, "POST", "/api/games/%s/heartbeat" % gid, {"token": token})
        call(base, "DELETE", "/api/games/%s" % gid, {"token": token})
        print("sgw: withdrawn", flush=True)
        return 0
    except (urllib.error.URLError, OSError) as e:
        print("sgw: cannot reach %s: %s" % (base, e), file=sys.stderr)
        return 4


def cli():
    """The `sgw` command a pip install puts on PATH."""
    sys.exit(main())


if __name__ == "__main__":
    cli()
