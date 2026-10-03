# Serious Games Week — matchmaker

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
