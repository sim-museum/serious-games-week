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

    def test_start_allowed_only_in_todays_category(self):
        game, tz, wd = self.today_game_and_tz()
        code, j = self.call("POST", "/api/games", {"game": game, "title": "t", "port": 47000, "tz": tz})
        self.assertEqual(code, 201, j)
        other = self.cats[(wd + 1) % 7]["games"][0]["id"]
        code, j = self.call("POST", "/api/games", {"game": other, "title": "t", "port": 47001, "tz": tz})
        self.assertEqual(code, 403, j)
        self.assertEqual(j["today"], self.cats[wd]["id"])

    def test_day_is_the_players_own(self):
        """The same game can be startable for a player in one zone and not for one in another."""
        for wd in range(7):
            tz_here, tz_there = zone_where_it_is(wd), zone_where_it_is((wd + 1) % 7)
            if tz_here and tz_there:
                game = self.cats[wd]["games"][0]["id"]
                self.assertEqual(self.call("POST", "/api/games", {"game": game, "port": 47002, "tz": tz_here})[0], 201)
                self.assertEqual(self.call("POST", "/api/games", {"game": game, "port": 47003, "tz": tz_there})[0], 403)
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
        self.assertEqual([g["name"] for g in st.list()], [""])

    def test_page_tells_players_how_to_point_their_games_here(self):
        with urllib.request.urlopen(self.base + "/") as r:
            page = r.read().decode()
        self.assertIn("sgw url " + self.base, page)
        self.assertIn("pronounced &ldquo;squeak&rdquo;", page)

    def test_chat_says_and_reads_since(self):
        time.sleep(1.05)   # the rate limit is per address, and every test client is 127.0.0.1
        first = self.call("GET", "/api/chat")[1]["messages"]
        since = first[-1]["id"] if first else 0
        code, j = self.call("POST", "/api/chat", {"name": "  Ace  ", "text": "anyone for a\nscramble?"})
        self.assertEqual(code, 201, j)
        new = self.call("GET", "/api/chat?since=%d" % since)[1]["messages"]
        self.assertEqual([(m["name"], m["text"]) for m in new], [("Ace", "anyone for a scramble?")])
        self.assertEqual(self.call("GET", "/api/chat?since=%d" % new[-1]["id"])[1]["messages"], [])

    def test_chat_rate_limit_and_validation(self):
        time.sleep(1.05)
        self.assertEqual(self.call("POST", "/api/chat", {"name": "B", "text": "one"})[0], 201)
        self.assertEqual(self.call("POST", "/api/chat", {"name": "B", "text": "two"})[0], 429)
        time.sleep(1.05)
        self.assertEqual(self.call("POST", "/api/chat", {"name": "", "text": "x"})[0], 400)
        code, _ = self.call("POST", "/api/chat", {"name": "C" * 99, "text": "y" * 999})
        self.assertEqual(code, 201)
        m = self.call("GET", "/api/chat")[1]["messages"][-1]
        self.assertEqual((len(m["name"]), len(m["text"])), (24, 300))

    def test_chat_keeps_only_the_newest(self):
        st = server.Store(os.path.join(self.tmp, "chat.db"))
        st.CHAT_KEEP = 5
        for i in range(12):
            st.say("n", "m%d" % i, "a")
        self.assertEqual([m["text"] for m in st.chat()], ["m%d" % i for i in range(7, 12)])

    def test_page_has_the_chat_panel(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn('id=chat', r.read().decode())

    def test_page_lists_every_category_equally(self):
        with urllib.request.urlopen(self.base + "/") as r:
            page = r.read().decode()
        for c in self.cats:
            self.assertIn(c["name"].replace("&", "&amp;"), page)
        self.assertEqual(page.count("<section"), 7)


if __name__ == "__main__":
    unittest.main()
