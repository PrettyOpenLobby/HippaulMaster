# CrystalMaster

A server reimplementation for Square Enix's Tetra Master, the online card game
of the PlayOnline service (2002-2010). Together with the OpenLobby core it
lets an unmodified client enter the zones and rooms, sit at a table, play
another player or the computer, trade, buy packs and sell cards, list cards
at auction, and see the weekly rankings, with no connection to Square Enix.
The PC client is the one this has been played on most; the PlayStation 2
client speaks the same protocol and is served too.

This project is a clean-room reimplementation based on protocol observation.
It contains no Square Enix code, art, or data: the four parameter tables the
server needs (card stats, prices, computer opponents, packs) are read out of
YOUR OWN client install by a tool in this repository, and every other file
the client fetches is built by the server.

## How it fits the core

Tetra Master has no server port of its own. The game rides the connection
the client already holds to the core's login service, plus the lobby's
resource fetches, so this title runs INSIDE the core's `login` and
`authsess` processes as a plugin (OpenLobby's `services/titles.py`,
`POL_TITLES=tmtitle`). This repository ships an image layered on the core's,
a compose override that swaps it into those two services, the weekly ranking
job, and an optional live board.

## Prerequisites

- The OpenLobby core, checked out beside this repository and already built
  once (`docker compose up -d --build` in that checkout)
- A Tetra Master client install of your own (the PlayOnline Viewer's
  `SquareEnix/TetraMaster` directory; US, EU and JP installs all work)
- Docker with Compose v2, and Python 3.10+ on the host for the one
  extraction step

## Bring-up

```
# 1. the client's parameter tables, decoded from YOUR install (once):
python tools/tmdata_build.py --client "C:/Program Files (x86)/PlayOnline/SquareEnix/TetraMaster"

# 2. the services, applied on top of the core's compose file, in its project:
cp .env.example .env
docker compose --project-directory ../openlobby \
    -f ../openlobby/docker-compose.yml -f docker-compose.yml up -d --build
```

Step 1 writes `services/tmdata/`: `CardPri.BIN` (the sell-price ladder),
`CardPrm.BIN` (one row per card: attack, type, defences, level), `CoPrm.BIN`
(the VS. COM opponents), `PackPrm.BIN` (the packs), and, from an English
install, `card_names_en.txt` (the card names, from the table's own string
pool). The files in your install are LZSS-compressed; the tool decodes them
with the client's own scheme. Nothing here ships with the repository.

Step 2 rebuilds the core's `login` and `authsess` containers from the
`crystalmaster` image (the core image plus this title) and starts the
`tmrank` job. The long command is the price of running inside the core's
project; put it in a shell alias, or set `COMPOSE_FILE` and
`COMPOSE_PROJECT_NAME` in your environment. To take the title out again,
run the core's own `docker compose up -d` from its checkout.

## Pointing a client at it

Everything client-side is the core's: DNS or hosts redirection, the CA, an
account. Grant the account Tetra Master (content id 2) in the admin panel,
start the Viewer, and pick Tetra Master from the games menu; the title's
version check is answered by the core's patch service. The client then
dials the address in `POL_TM_ZONE_HOST` (or the core's `POL_ADVERTISE`) for
its zones, so that must be an address the client can route to.

## The weekly rankings

The five ranking lists are rebuilt once a week, Sunday 00:05 UTC, by the
`tmrank` service (a loop around `tools/tmrank.py --publish`). Set
`TM_RANK_AT=now` to publish at start as well, for a fresh server. The lists
are served from `/data/resources/tmrank/`; until the first publish the
client sees one empty row, which is what its rankings screen needs to open.

## The live board (optional)

`docker compose ... --profile board up -d` adds a read-only web page of the
standings, the auction and the matches in progress on port 8793 (bound to
localhost; put a reverse proxy in front). It draws with art baked from your
own client: run `python tools/tm_boardart_bake.py --client <install>` first
(the fonts it uses are OFL-licensed and ship here). Discord posting is
configured with the `POL_BOARDS_TM_*` variables in `.env.example`.

## Selftests

```
python tools/tm_run_all.py
```

runs the offline suite; it needs the OpenLobby checkout beside this one (or
`OPENLOBBY_SERVICES` pointing at its `services/` directory). The suites that
draw the board skip until the art has been baked.

## Events

An event (the in-game event board and shop) is off until a window is set:
`POL_TM_EVENT_START`/`POL_TM_EVENT_END` in unix seconds, and the board's
rows in `POL_TM_EVENT_MEMBERS` (`hexid:name:score,...`). With no window the
client reads "no event" and never opens those screens.

## What is not included, and why

- No Square Enix data: the parameter tables are decoded from your own
  client; the card and board art the live board draws is baked from it.
- No captured server files: the zone list, room lists, table list, auction
  counts, ranking header and event schedule are built by the server from
  the layouts the client's readers dictate and this server's own content.
- The Janhourou rooms that share the lobby's list format are that title's,
  not this one's.

## License

AGPL-3.0 (see LICENSE). If you run a modified version as a service, the
license obliges you to offer your modifications' source to its users.

## Credits

- The Tetra Master community's records of the original service, from
  which the zone names and the weekly rebuild schedule were taken.
- The PlayOnline preservation community.
- Almendra and M PLUS 1p (SIL Open Font License); the derived faces in
  `services/boardart/tm/` keep the OFL's Reserved Font Name terms.
