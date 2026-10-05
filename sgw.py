#!/usr/bin/env python3
"""sgw -- the Serious Games Week matchmaker client the games call (stdlib only).

The matchmaker URL is the player's choice: $SGW_URL, else the first line of ~/.config/sgw/url.
  sgw url                                      print the configured matchmaker
  sgw url https://games.example.org            choose the matchmaker (writes ~/.config/sgw/url)
  sgw today                                    today's category where you are (and its games)
  sgw list --game ma [--json]                  joinable sessions: one "host port players title" line each
  sgw announce --game ma --port 47734 --title "Spring Offensive" [--name N --players N --max N --version V --build B]
                                               list this host's game and keep it listed (heartbeat) until
                                               killed (SIGTERM/SIGINT) or stdin closes -- the game spawns it when
                                               it starts hosting and kills it when the session ends. Exit 3 and
                                               a message on stderr if today's category does not allow the game.
  sgw chat [--follow]                          the lobby chat (--follow keeps printing new messages)
  sgw say "text" [--name N]                    say something in the lobby chat
Exit codes: 0 ok, 2 no matchmaker configured, 3 refused by the matchmaker, 4 network error.
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

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
    c = sub.add_parser("chat"); c.add_argument("--follow", action="store_true")
    y = sub.add_parser("say"); y.add_argument("text")
    y.add_argument("--name", default=os.environ.get("SGW_NAME") or os.environ.get("USER", ""))
    a = sub.add_parser("announce")
    a.add_argument("--game", required=True); a.add_argument("--port", type=int, required=True)
    a.add_argument("--title", default=""); a.add_argument("--players", type=int, default=1)
    a.add_argument("--max", type=int, default=0); a.add_argument("--version", default="")
    a.add_argument("--build", default=os.environ.get("SGW_BUILD", ""), help="git commit of this build ($SGW_BUILD)")
    a.add_argument("--host", default=None, help="address players should use (default: as the matchmaker sees you)")
    a.add_argument("--name", default=os.environ.get("SGW_NAME") or os.environ.get("USER", ""),
                   help="your name as other players see it (default: $SGW_NAME, else your login)")
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
            if args.json:
                print(json.dumps(j["games"]))
            else:
                for g in j["games"]:
                    print("%s %d %d %s" % (g["host"], g["port"], g["players"], g["title"].replace("\n", " ")))
            return 0
        if args.cmd in ("chat", "say"):
            if args.cmd == "say":
                code, j = call(base, "POST", "/api/chat", {"name": args.name, "text": args.text})
                if code != 201:
                    print("sgw: %s" % j.get("error"), file=sys.stderr); return 3
                return 0
            last = 0
            while True:
                code, j = call(base, "GET", "/api/chat?since=%d" % last)
                for m in j.get("messages", []):
                    last = m["id"]
                    print("%s  %s: %s" % (time.strftime("%H:%M", time.localtime(m["ts"])), m["name"], m["text"]), flush=True)
                if not args.follow:
                    return 0
                time.sleep(3)
        # announce
        body = dict(where(), game=args.game, port=args.port, title=args.title, players=args.players,
                    max_players=args.max, version=args.version, name=args.name, build=args.build)
        if args.host:
            body["host"] = args.host
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
