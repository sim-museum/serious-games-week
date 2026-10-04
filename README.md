# squeak — Serious Games Week

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

## The games
squeak is the public face of a long-running serious-games research project (sim-museum). Each game lives in its own
repository:

| Day | Game | Repository | Plays on squeak |
|---|---|---|---|
| Monday | pokerIQ | [serious-games-lab](https://github.com/sim-museum/serious-games-lab/tree/freefalcon-buildscript-fixes/MON/pokerIQ) (`MON/pokerIQ`) | yes |
| Tuesday | MiG Alley (Rowan, native Linux port) | [mig_src](https://github.com/sim-museum/mig_src) | yes |
| Tuesday | Battle of Britain (Rowan, native Linux port) | [BOB_Src](https://github.com/sim-museum/BOB_Src) | yes |
| Wednesday | chessIQ (Kramnik's no-castling chess) | [serious-games-lab](https://github.com/sim-museum/serious-games-lab/tree/freefalcon-buildscript-fixes/WED/chessIQ) (`WED/chessIQ`) | yes |
| Thursday | Julia Racer | [serious-games-lab](https://github.com/sim-museum/serious-games-lab/tree/julia-racer/THU) (`THU`, branch `julia-racer`) | yes |
| Friday | bridgeIQ | [serious-games-lab](https://github.com/sim-museum/serious-games-lab/tree/freefalcon-buildscript-fixes/FRI/bridgeIQ) (`FRI/bridgeIQ`) | yes |
| Saturday | FreeFalcon | [freefalcon-central](https://github.com/sim-museum/freefalcon-central) | yes |
| Sunday | KaTrain (Go) | [katrain](https://github.com/sim-museum/katrain), a fork of sanderland/katrain with network play | yes |

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

## Install sgw (every player's machine)
`sgw` is the small client the games call. It uses only the Python standard library (3.8+):

    pipx install git+https://github.com/sim-museum/squeak       # or: pip install --user git+https://github.com/sim-museum/squeak
    sgw --version

This puts `sgw` on your PATH, where every game looks for it. The flight-sim AppImages carry their own copy, and a
clone of this repo at `~/squeak` or `~/sgweek` also works. Only the client is installed; to run a matchmaker, use a
clone (below).

## Point the games at it
    sgw url https://your-matchmaker.example      # writes ~/.config/sgweek/url; or export SGW_URL=...
The games call `sgw` (`sgw announce` while hosting, `sgw list` to find games). Hosts must accept incoming
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

## How to host a game
You host **in the game**, not on the website. The matchmaker lists your game for you.
1. Once per machine, point it at the matchmaker: `sgw url http://<matchmaker-host>:8090`.
2. On the day whose category includes your game (where you are), host as usual:
   * **MiG Alley / Battle of Britain:** Multi-Player → Create Game → pick a game type → Continue. For a co-op campaign,
     the session opens when you reach the campaign ready room.
   * **FreeFalcon:** Comms → Connect with **no remote address** (listening).
3. The game announces itself, and the page shows it within 10 s. It stays listed while you host (a heartbeat every
   30 s) and disappears when you close the session or quit.
4. Players join from the game's own Join screen (MA/BoB) or Comms phonebook (FF), where listed hosts appear.

If today's category does not include your game, the matchmaker refuses the listing and the game's log says why. You
can still host for players who know your address. On a LAN nothing more is needed. Over the internet, forward the
game's port on your router: UDP 47624 for MA and BoB (MA_DPLAY_PORT / BOB_DPLAY_PORT change it), 2934 for FF.

Games without built-in support can be listed by hand while you host them:
`sgw announce --game katrain --port 6000 --title "Teaching game"` (Ctrl-C withdraws it).

## Lobby chat
Everyone on the page shares one chat, kept to the newest 1000 messages, one message a second per address. Your name
is remembered by your browser. From a terminal: `sgw chat --follow` and `sgw say "anyone for a scramble?"`.

## API
See the docstring at the top of `server.py`. Tests: `python3 -m unittest -v test_server`.
