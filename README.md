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
    echo https://your-matchmaker.example > ~/.config/sgweek/url     # or export SGW_URL=...
The games call `sgw.py` (`sgw announce` while hosting, `sgw list` to find games). Hosts must accept incoming
connections on the game's port (port-forward it), exactly as with iGOR.

## API
See the docstring at the top of `server.py`. Tests: `python3 -m unittest -v test_server`.
