# Serious Games Week — matchmaker

**sgweek** is pronounced **"squeak"**. It is short for **Serious Games Week**.

The name is also a reminder of how to play. Set each game's strength (the opponent's level, the AI's skill, the
handicap) so that you **barely succeed**: you only just *squeak by*. A win that costs nothing teaches nothing, and a
loss you could never avoid teaches little more. The learning happens at the edge of your ability, so keep yourself
there.

An iGOR-style lobby for a weekly collection of **Linux-native serious games**: one category for each day of the
week. You can **start** a game only in **today's** category *where you are* (your own time zone); you can **join**
any game that is running. Everyone rotates through all seven categories, and the site promotes no game over another.

| Day | Category | Games |
|---|---|---|
| Monday | Poker | pokerIQ |
| Tuesday | Air war: Korea and the Battle of Britain | MiG Alley, Battle of Britain |
| Wednesday | Chess | Kramnik chess |
| Thursday | Motor racing | Julia Racer |
| Friday | Bridge | bridgeIQ |
| Saturday | Modern air combat | FreeFalcon |
| Sunday | Go | KaTrain |

(Edit `categories.json` to change the week.)

## Run a matchmaker
    python3 server.py --port 8080 --db sgweek.db      # Python 3.9+, no other dependencies
Put it behind any HTTPS reverse proxy if it faces the internet. Players point their games at it.

## Host it on your LAN (systemd user service)
    cp deploy/sgweek.service ~/.config/systemd/user/
    systemctl --user daemon-reload && systemctl --user enable --now sgweek
    loginctl enable-linger $USER        # optional: keep it up while nobody is logged in
It listens on port 8090. The web page (`http://<this machine>:8090/`) is the live lobby: every day's category,
today's highlighted with a countdown to its end in the viewer's own time zone, and every running session with game,
title, host player, address, players and age. It refreshes every 10 s.

## Point the games at it
    ./sgw.py url https://your-matchmaker.example      # writes ~/.config/sgweek/url; or export SGW_URL=...
The games call `sgw.py` (`sgw announce` while hosting, `sgw list` to find games). Hosts must accept incoming
connections on the game's port (port-forward it), exactly as with iGOR.

How each game uses it:
* **MiG Alley and Battle of Britain:** hosting a multiplayer session lists it, and closing the session withdraws it.
  Join's session list also shows the sessions the matchmaker lists.
* **FreeFalcon:** going online in the Comms window *without* a remote address lists you. The hosts the matchmaker
  lists appear in the Comms phonebook; pick one and Connect.
* If today's category where you are does not include the game, the matchmaker refuses the listing. The game still
  hosts on your LAN, and its log says why.
* **Day check:** the day is taken from the time zone the player's machine reports. Like iGOR's rules, it is a
  convention among players, not a lock.

## API
See the docstring at the top of `server.py`. Tests: `python3 -m unittest -v test_server`.
