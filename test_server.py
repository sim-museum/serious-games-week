#!/usr/bin/env python3
"""Tests for the Serious Games Week matchmaker: python3 -m unittest -v test_server"""
import datetime
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server

HERE = os.path.dirname(os.path.abspath(__file__))


def zone_where_it_is(weekday):
    """An IANA zone whose local weekday right now is `weekday` (0 = Monday), or None."""
    for tz in ["Pacific/Kiritimati", "Pacific/Auckland", "Asia/Tokyo", "Europe/Berlin", "UTC",
               "America/New_York", "America/Los_Angeles", "Pacific/Honolulu", "Pacific/Pago_Pago"]:
        if server.local_weekday(tz) == weekday:
            return tz
    return None


class MatchmakerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.cats = server.load_categories(os.path.join(HERE, "categories.json"))
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(server.Store(os.path.join(cls.tmp, "t.db")), cls.cats))
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def today_game_and_tz(self):
        tz = "UTC"
        wd = server.local_weekday(tz)
        return self.cats[wd]["games"][0]["id"], tz, wd

    def test_week_has_seven_categories(self):
        code, j = self.call("GET", "/api/categories")
        self.assertEqual(code, 200)
        self.assertEqual([c["weekday"] for c in j["categories"]], list(range(7)))

    def test_any_game_any_day(self):
        """The day's category is a suggestion (PO, 2026-10-08): another day's game is accepted, and flagged."""
        game, tz, wd = self.today_game_and_tz()
        code, j = self.call("POST", "/api/games", {"game": game, "title": "t", "port": 47000, "tz": tz})
        self.assertEqual(code, 201, j)
        self.assertTrue(j["on_day"])
        other = self.cats[(wd + 1) % 7]["games"][0]["id"]
        code, j = self.call("POST", "/api/games", {"game": other, "title": "t", "port": 47001, "tz": tz})
        self.assertEqual(code, 201, j)
        self.assertFalse(j["on_day"])
        self.assertEqual(j["today"], self.cats[wd]["id"])
        listed = [g["game"] for g in self.call("GET", "/api/games")[1]["games"]]
        self.assertIn(other, listed)

    def test_day_is_the_players_own(self):
        """The same game can be on the day's category for a player in one zone and not for one in another."""
        for wd in range(7):
            tz_here, tz_there = zone_where_it_is(wd), zone_where_it_is((wd + 1) % 7)
            if tz_here and tz_there:
                game = self.cats[wd]["games"][0]["id"]
                self.assertTrue(self.call("POST", "/api/games", {"game": game, "port": 47002, "tz": tz_here})[1]["on_day"])
                self.assertFalse(self.call("POST", "/api/games", {"game": game, "port": 47003, "tz": tz_there})[1]["on_day"])
                return
        self.skipTest("no two zones on adjacent weekdays right now")

    def test_list_heartbeat_withdraw_and_host_from_request(self):
        game, tz, _ = self.today_game_and_tz()
        code, j = self.call("POST", "/api/games", {"game": game, "title": "hb", "port": 47010, "tz": tz, "players": 2})
        gid, token = j["id"], j["token"]
        games = self.call("GET", "/api/games?game=" + game)[1]["games"]
        mine = [g for g in games if g["id"] == gid][0]
        self.assertEqual(mine["host"], "127.0.0.1")          # what other players must reach
        self.assertNotIn("token", mine)                       # the token never leaves the server
        self.assertEqual(self.call("POST", "/api/games/%s/heartbeat" % gid, {"token": token, "players": 3})[1]["ok"], True)
        self.assertEqual(self.call("POST", "/api/games/%s/heartbeat" % gid, {"token": "wrong"})[0], 404)
        self.assertEqual(self.call("DELETE", "/api/games/%s" % gid, {"token": token})[1]["ok"], True)
        self.assertFalse([g for g in self.call("GET", "/api/games")[1]["games"] if g["id"] == gid])

    def test_unheartbeated_session_expires(self):
        game, tz, _ = self.today_game_and_tz()
        gid = self.call("POST", "/api/games", {"game": game, "port": 47020, "tz": tz})[1]["id"]
        old, server.EXPIRE_S = server.EXPIRE_S, 0
        try:
            time.sleep(0.05)
            self.assertFalse([g for g in self.call("GET", "/api/games")[1]["games"] if g["id"] == gid])
        finally:
            server.EXPIRE_S = old

    def test_unknown_game_and_missing_zone_refused(self):
        self.assertEqual(self.call("POST", "/api/games", {"game": "nope", "port": 1, "tz": "UTC"})[0], 400)
        game, _, _ = self.today_game_and_tz()
        self.assertEqual(self.call("POST", "/api/games", {"game": game, "port": 1})[0], 400)

    def test_name_and_age_are_listed(self):
        game, tz, _ = self.today_game_and_tz()
        gid = self.call("POST", "/api/games", {"game": game, "port": 47030, "tz": tz, "name": "Ace", "max_players": 16})[1]["id"]
        mine = [g for g in self.call("GET", "/api/games")[1]["games"] if g["id"] == gid][0]
        self.assertEqual(mine["name"], "Ace")
        self.assertEqual(mine["max_players"], 16)
        self.assertGreaterEqual(mine["age_s"], 0)

    def test_old_database_gains_the_name_column(self):
        import sqlite3
        path = os.path.join(self.tmp, "old.db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE games (id TEXT PRIMARY KEY, token TEXT, game TEXT, category TEXT, title TEXT, host TEXT,"
                   " port INTEGER, version TEXT, players INTEGER, max_players INTEGER, started REAL, seen REAL)")
        db.execute("INSERT INTO games VALUES ('x','t','ma','wings','old','1.2.3.4',1,'',1,0,?,?)", (time.time(), time.time()))
        db.commit(); db.close()
        st = server.Store(path)
        self.assertEqual([g["name"] for g in st.list()[0]], [""])

    def test_version_skew_only_same_build_is_listed(self):
        """Two builds from different commits are never matched: a caller that names its build sees only sessions
        from the same commit, and is told how many others it does not see."""
        game, tz, _ = self.today_game_and_tz()
        mk = lambda title, build: self.call("POST", "/api/games", dict({"game": game, "title": title, "port": 47100,
                                                                        "tz": tz}, **({"build": build} if build else {})))[1]
        a, b, c = mk("same", "abc1234"), mk("other", "def5678"), mk("unknown", None)
        j = self.call("GET", "/api/games?game=%s&build=abc1234" % game)[1]
        titles = [g["title"] for g in j["games"]]
        self.assertIn("same", titles)
        self.assertNotIn("other", titles)
        self.assertNotIn("unknown", titles)                  # no build: cannot be checked, so not offered
        self.assertGreaterEqual(j["hidden_other_builds"], 2)
        self.assertEqual([g["build"] for g in j["games"] if g["title"] == "same"], ["abc1234"])
        everything = [g["title"] for g in self.call("GET", "/api/games?game=" + game)[1]["games"]]
        self.assertTrue({"same", "other", "unknown"} <= set(everything))   # an older client still sees all
        for x in (a, b, c):
            self.call("DELETE", "/api/games/%s" % x["id"], {"token": x["token"]})

    def test_page_tells_players_how_to_point_their_games_here(self):
        with urllib.request.urlopen(self.base + "/") as r:
            page = r.read().decode()
        self.assertIn("sgw url " + self.base, page)
        self.assertIn("so that you barely succeed", page)

    def test_note_is_one_line_listed_and_shown(self):
        day, tz, _ = self.today_game_and_tz()
        code, j = self.call("POST", "/api/games", {"game": day, "port": 47020, "title": "Evening", "tz": tz,
                                                   "note": "  beginners\nwelcome, <b>1v1</b> " + "x" * 200})
        self.assertEqual(code, 201)
        g = [g for g in self.call("GET", "/api/games?game=" + day)[1]["games"] if g["id"] == j["id"]][0]
        self.assertTrue(g["note"].startswith("beginners welcome, <b>1v1</b> x"))
        self.assertEqual(len(g["note"]), 120)
        with urllib.request.urlopen(self.base + "/") as r:
            page = r.read().decode()
        self.assertIn("beginners welcome, &lt;b&gt;1v1&lt;/b&gt;", page)
        self.assertNotIn("<b>1v1</b>", page)

    def start(self, **kw):
        body = dict({"game": "ma", "title": "Chat test", "port": 4243, "tz": "UTC", "name": "Host"}, **kw)
        code, j = self.call("POST", "/api/games", body)
        self.assertEqual(code, 201)
        return j

    def test_there_is_no_lobby_chat(self):
        self.assertEqual(self.call("GET", "/api/chat")[0], 410)
        self.assertEqual(self.call("POST", "/api/chat", {"name": "A", "text": "hi"})[0], 410)

    def test_chat_is_inside_a_game_for_its_members(self):
        g = self.start()
        path = "/api/games/%s/chat" % g["id"]
        self.assertEqual(self.call("GET", path)[0], 403)                       # not joined: nothing to read
        self.assertEqual(self.call("POST", path, {"token": "x", "text": "hi"})[0], 403)
        code, j = self.call("POST", "/api/games/%s/join" % g["id"], {"name": "  Ace  "})
        self.assertEqual(code, 200)
        self.assertEqual(j["port"], 4243)
        code, _ = self.call("POST", path, {"token": j["member"], "text": "anyone for a\nscramble?"})
        self.assertEqual(code, 201)
        time.sleep(1.05)
        self.assertEqual(self.call("POST", path, {"token": g["token"], "text": "yes"})[0], 201)   # the host chats too
        ms = self.call("GET", path + "?token=" + j["member"])[1]["messages"]
        self.assertEqual([(m["name"], m["text"]) for m in ms], [("Ace", "anyone for a scramble?"), ("Host", "yes")])
        self.assertEqual(self.call("GET", path + "?token=%s&since=%d" % (j["member"], ms[-1]["id"]))[1]["messages"], [])
        other = self.start(title="Other")                                      # another game sees none of it
        code, k = self.call("POST", "/api/games/%s/join" % other["id"], {"name": "B"})
        self.assertEqual(self.call("GET", "/api/games/%s/chat?token=%s" % (other["id"], k["member"]))[1]["messages"], [])
        self.assertEqual(self.call("GET", path + "?token=" + k["member"])[0], 403)   # a member of one game only
        self.call("DELETE", "/api/games/" + g["id"], {"token": g["token"]})
        self.assertEqual(self.call("GET", path + "?token=" + j["member"])[0], 403)   # the chat ends with the game
        self.call("DELETE", "/api/games/" + other["id"], {"token": other["token"]})

    def test_chat_rate_limit_and_validation(self):
        g = self.start()
        _, j = self.call("POST", "/api/games/%s/join" % g["id"], {"name": "B"})
        path = "/api/games/%s/chat" % g["id"]
        time.sleep(1.05)
        self.assertEqual(self.call("POST", path, {"token": j["member"], "text": "one"})[0], 201)
        self.assertEqual(self.call("POST", path, {"token": j["member"], "text": "two"})[0], 429)
        time.sleep(1.05)
        self.assertEqual(self.call("POST", path, {"token": j["member"], "text": "  "})[0], 400)
        self.assertEqual(self.call("POST", "/api/games/%s/join" % g["id"], {"name": ""})[0], 400)
        self.assertEqual(self.call("POST", "/api/games/nosuch/join", {"name": "C"})[0], 404)
        self.call("DELETE", "/api/games/" + g["id"], {"token": g["token"]})

    def test_chat_keeps_only_the_newest_per_game(self):
        st = server.Store(os.path.join(self.tmp, "chat.db"))
        st.CHAT_KEEP = 5
        for i in range(12):
            st.say("g1", "n", "m%d" % i, "1.2.3.4")
        st.say("g2", "n", "other", "1.2.3.4")
        self.assertEqual([m["text"] for m in st.chat("g1")], ["m%d" % i for i in range(7, 12)])
        self.assertEqual([m["text"] for m in st.chat("g2")], ["other"])

    def test_password_hides_the_address_until_joined(self):
        g = self.start(title="Locked", password="s3cret", port=4999)
        row = [x for x in self.call("GET", "/api/games?game=ma")[1]["games"] if x["id"] == g["id"]][0]
        self.assertTrue(row["locked"])
        self.assertEqual((row["host"], row["port"]), ("", 0))
        self.assertEqual(self.call("POST", "/api/games/%s/join" % g["id"], {"name": "A"})[0], 403)
        self.assertEqual(self.call("POST", "/api/games/%s/join" % g["id"], {"name": "A", "password": "wrong"})[0], 403)
        code, j = self.call("POST", "/api/games/%s/join" % g["id"], {"name": "A", "password": "s3cret"})
        self.assertEqual((code, j["host"], j["port"]), (200, "127.0.0.1", 4999))
        self.assertNotIn("s3cret", open(os.path.join(self.tmp, "t.db"), "rb").read().decode("latin-1"))
        self.call("DELETE", "/api/games/" + g["id"], {"token": g["token"]})

    def test_open_game_is_listed_with_its_address(self):
        g = self.start(title="Open", port=4998)
        row = [x for x in self.call("GET", "/api/games?game=ma")[1]["games"] if x["id"] == g["id"]][0]
        self.assertFalse(row["locked"])
        self.assertEqual(row["port"], 4998)
        self.call("DELETE", "/api/games/" + g["id"], {"token": g["token"]})

    def test_any_number_of_games_at_once(self):
        gs = [self.start(title="Many %d" % i, port=5000 + i) for i in range(30)]
        listed = {x["id"] for x in self.call("GET", "/api/games?game=ma")[1]["games"]}
        self.assertTrue({g["id"] for g in gs} <= listed)
        for g in gs:
            self.call("DELETE", "/api/games/" + g["id"], {"token": g["token"]})

    def test_old_lobby_chat_database_is_upgraded(self):
        import sqlite3
        p = os.path.join(self.tmp, "lobby.db")
        db = sqlite3.connect(p)
        db.execute("CREATE TABLE chat (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, name TEXT, text TEXT, addr TEXT)")
        db.execute("INSERT INTO chat (ts,name,text,addr) VALUES (1,'a','old lobby line','x')")
        db.commit(); db.close()
        st = server.Store(p)
        self.assertEqual(st.chat(""), [])            # the lobby's lines are not carried into any game

    def test_page_has_join_and_no_lobby_chat(self):
        g = self.start(title="Page row", password="pw")
        with urllib.request.urlopen(self.base + "/") as r:
            page = r.read().decode()
        self.assertNotIn("Lobby chat", page)
        self.assertIn('class=join data-id=%s data-locked=1' % g["id"], page)
        self.call("DELETE", "/api/games/" + g["id"], {"token": g["token"]})

    def test_sgw_client_join_chat_and_locked_list(self):
        import subprocess, sys
        g = self.start(title="CLI", password="pw", port=4777)
        env = dict(os.environ, SGW_URL=self.base)
        run = lambda *a: subprocess.run([sys.executable, os.path.join(HERE, "sgw.py")] + list(a), env=env,
                                        capture_output=True, text=True, timeout=20)
        r = run("list", "--game", "ma")
        self.assertNotIn("4777", r.stdout)
        self.assertIn(g["id"], r.stderr)
        self.assertEqual(run("join", "--id", g["id"], "--name", "Cli").returncode, 3)
        r = run("join", "--id", g["id"], "--name", "Cli", "--password", "pw")
        host, port, tok = r.stdout.split()
        self.assertEqual((host, port), ("127.0.0.1", "4777"))
        time.sleep(1.05)
        self.assertEqual(run("say", "--id", g["id"], "--token", tok, "hello").returncode, 0)
        self.assertIn("Cli: hello", run("chat", "--id", g["id"], "--token", tok).stdout)
        self.call("DELETE", "/api/games/" + g["id"], {"token": g["token"]})

    def test_ping_responder_is_timed_and_listed(self):
        import sgw
        srv = sgw.ping_responder(0)
        pp = srv.server_address[1]
        req = urllib.request.Request("http://127.0.0.1:%d/ping" % pp)
        with urllib.request.urlopen(req) as r:          # what the page fetches: 204, open to any origin
            self.assertEqual((r.status, r.headers["Access-Control-Allow-Origin"]), (204, "*"))
        ms = sgw.ping_ms("127.0.0.1", pp)
        self.assertIsNotNone(ms)
        self.assertLess(ms, 50)
        g = self.start(title="Pinged", ping_port=pp)
        row = [x for x in self.call("GET", "/api/games?game=ma")[1]["games"] if x["id"] == g["id"]][0]
        self.assertEqual(row["ping_port"], pp)
        self.assertIsNotNone(sgw.add_pings([row])[0]["ping_ms"])
        srv.shutdown(); srv.server_close()
        self.assertIsNone(sgw.ping_ms("127.0.0.1", pp, timeout=0.5))     # a host that has gone: no ping, no hang
        self.assertEqual(self.call("POST", "/api/games", {"game": "ma", "port": 1, "tz": "UTC", "ping_port": 70000})[0], 400)
        self.call("DELETE", "/api/games/" + g["id"], {"token": g["token"]})

    def test_locked_game_ping_port_only_after_join(self):
        g = self.start(title="Locked ping", password="pw", ping_port=5555)
        row = [x for x in self.call("GET", "/api/games?game=ma")[1]["games"] if x["id"] == g["id"]][0]
        self.assertEqual(row["ping_port"], 0)
        self.assertEqual(self.call("POST", "/api/games/%s/join" % g["id"], {"name": "A", "password": "pw"})[1]["ping_port"], 5555)
        self.call("DELETE", "/api/games/" + g["id"], {"token": g["token"]})

    def test_announce_runs_a_ping_responder(self):
        import subprocess, sys
        env = dict(os.environ, SGW_URL=self.base)
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "sgw.py"), "announce", "--game", "ma", "--port", "4321",
                              "--title", "Announced"], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertIn("listed as", p.stdout.readline())
        row = [x for x in self.call("GET", "/api/games?game=ma")[1]["games"] if x["title"] == "Announced"][0]
        self.assertGreater(row["ping_port"], 0)
        r = subprocess.run([sys.executable, os.path.join(HERE, "sgw.py"), "list", "--game", "ma", "--json"], env=env,
                           capture_output=True, text=True, timeout=20)
        mine = [x for x in json.loads(r.stdout) if x["title"] == "Announced"][0]
        self.assertIsNotNone(mine["ping_ms"])
        p.stdin.close(); p.wait(10)

    def test_page_lists_every_category_equally(self):
        with urllib.request.urlopen(self.base + "/") as r:
            page = r.read().decode()
        for c in self.cats:
            self.assertIn(c["name"].replace("&", "&amp;"), page)
        self.assertEqual(page.count("<section"), 7)


if __name__ == "__main__":
    unittest.main()
