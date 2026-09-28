# Contributing to CrystalMaster

CrystalMaster is the Tetra Master title for the OpenLobby core. It runs inside
the core's `login` and `authsess` processes as a plugin, so most of what a
client does in Tetra Master arrives here as one line of the game channel and
leaves as the line that answers it. This page says where things are, how to
run the checks, and what a pull request needs.

## Where things are

```
services/
  tmtitle.py        the plugin's public name (POL_TITLES=tmtitle) and a facade
                    over tmplugin/; see below
  tmplugin/         the plugin the core loads: the seam that hands the core's
                    traffic to the game, plus the room roster, auction,
                    rankings, zones and the lobby blobs (tmplugin/__init__.py
                    lists the modules)
  tetramaster.py    the game server's public name and a facade over tmgame/
  tmgame/           the game server, one module per concern
                    (tmgame/__init__.py lists them)
  tmroom.py         the live room roster behind b/g/PTL (shared with Janhourou)
  tmbattle.py       board geometry, arrows and the battle resolver
  tmsave.py         the player save (U/g/TM0DataFile): author and decode it
  tmroll.py         a card's rolled stats and how they grow with use
  tmrank.py         the weekly rankings: the rank rows and their header
  tmauction.py      the auction's exhibit record and listing store
  tmprize.py        prize points and the weekly lucky cards
  tmcup.py          tournaments: which event, its phase, missions and prizes
  tmeventstate.py   tournament standings shared between the two processes
  tm_title.py       the player titles (CoPrm.BIN) and the client's picker
  tm_cardprm.py     card parameters from your install's CardPrm.BIN
  tmfixtures.py     the lobby and default-data blobs the client fetches
  tmtables.py       the table-truth report
  tmstore.py        reaches the core's polcore: the durable tables and the
                    live keys (tm:*); migrate and status
  tm_migrations/    CrystalMaster's migrations, numbered from 3001
  tmblob.py         collections, saves, prizes, auction records and rank
                    lists as rows of the core's blob table
  boardtm.py        the optional live board (rankings, auction, matches)
  polboards.py      the board service and its Discord posting
  polgateway.py     the Discord Gateway presence for the board bot
  tmdata/           the client's parameter tables, decoded from YOUR install
                    by tools/tmdata_build.py (only card_names_en.txt ships)
  boardart/         the board's fonts; its art is baked from your install
tools/              self-tests (`*_test.py`), the runner (tm_run_all.py),
                    the test database helper (tmpg.py) and operator tools
                    (tmrank_job.py, tm_money_reset.py, ...)
config/polpro.json  reply templates for the POLpro plaintext channel
```

A line from a Tetra Master client reaches the core first. The core hands it to
the plugin (`tmplugin/plugin.py`, the `titles.Title` subclass), and a game
line goes on through `tmplugin/envelope.py` to `tetramaster.handle_line` in
`tmgame/dispatch.py`. Lobby-band fetches (zone and room lists, `b/g/PTL`,
the manifests) stay in `tmplugin/`.

`tmplugin/` in brief: `plugin.py` is every hook the core calls and the place
to start; `corenames.py` binds the core names the plugin uses (`log`,
`_session_get`, `ROOMS`, ...) from `titles.core`, and the other modules refer
to them as `corenames.<name>`; `envelope.py` is the auth band's game channel;
`roster.py`, `auction.py`, `rankings.py`, `pool.py` and `chat.py` are the
auth-band record families; `fetches.py`, `manifests.py`, `zones.py`,
`ptl.py`, `events.py`, `templates.py` and `savedefaults.py` are the lobby
band's resources.

`tmgame/` is split along the game. Reading order for a first visit:

1. `protocol.py`: the message codes of the TM0 game channel and the E-body
   header every line starts with. Its comments are the protocol reference.
2. `dispatch.py`: `handle_line` and the `_handle_line` switch, where every
   command the client sends is recognised and handed on. It is one function
   of about 3,000 lines; search it for a command name (`@GameEN=`,
   `@PutData=`) to find the module that does the work.
3. The shop scene: `cardshop.py` (packs, buying, selling), `shopdoors.py`
   (the scene's `@Init` bodies: card shop, auction Check Out, event shop),
   `purse.py` (the gil balance), `cardtables.py` (the parameter tables).
4. A member's cards: `collection.py` (the collection file and decks),
   `savefile.py` (patching the save the client reads), `trade.py`.
5. The lobby side of a table: `reservation.py`, `seating.py`,
   `tablesettings.py`, `tablerow.py`, `tableaudit.py`, `peers.py`,
   `roomchat.py`, `opener.py` (the first exchange, `@TeachDV`).
6. A match, in the order it happens: `matchmaking.py` (from seats to
   `@MuchMake` and the accept quorum), `pushqueue.py` (how a server-initiated
   line reaches a client), `matchstart.py` and `ruleset.py` (the deal),
   `boardrules.py` and `placement.py` (playing a card), `scoring.py`,
   `matchend.py` and `careerstats.py` (the result), `rematch.py`, `turns.py`
   (turn flow and a bot taking a departed seat), `pots.py` (stakes),
   `watchers.py` (spectators in the client).
7. `vscom.py` (playing the computer), `tournament.py` (events),
   `webwatch.py` (the files the web board reads).
8. `selftest_*.py`: the selftest suite, run by `python tetramaster.py
   --selftest`.

In both packages `deps.py` holds the imports the modules share; in `tmgame/`
that includes the optional siblings (`tmauction`, `tmsave`, `tmprize`,
`tmroll`), which are `None` when missing. `tmgame/common.py` holds `_say`, the
log line every game module uses.

### The facades

`services/tetramaster.py` is where the whole game server used to live, and
`services/tmtitle.py` the whole plugin. Each is now a thin module that
imports its package and forwards `<module>.<name>` reads and writes to the
module that owns the name. The core (`POL_TITLES=tmtitle`), `tmtables`, the
tools and the tests keep using `import tetramaster` and `import tmtitle`,
including the tests that rebind a name to quiet or fake a helper
(`tm._say = lambda *a, **k: None`, `R._live_rooms = lambda: live`): the write
lands in the owning module, so the code under test sees it.
`tools/facade_rebind_check.py` proves the forwarding holds for every
rebinding the tools make. New code inside a package refers to a sibling as
`<module>.<name>`.

### Changing the packages

`tmgame/` was generated once from the single-file `tetramaster.py` in commit
4161062, and `tmplugin/` from the single-file `tmtitle.py` in commit
f8ac59d. The packages are the source now and are edited directly; nothing
regenerates them. Code written against a single file elsewhere is carried
over by hand into the module that owns that code today.

`tetramaster.py` and `tmtitle.py` stay as the names everything imports and
as the facades described above. Each one forwards only the names in its
`_OWNERS` table, which maps every name to the module that owns it, so a new
top-level name is not reachable as `tetramaster.NAME` or `tmtitle.NAME`
until it has a line there. Package code does not need one, since it uses
`<module>.<name>`. A tool, a test or the core that reads or rebinds the name
through the facade does, and `tools/facade_rebind_check.py` fails on a
rebinding in `tools/` or `services/` of a name the table does not list. A
new module is imported at the top of the facade and added to `_MODULES`. A
core name the plugin starts to use is added to `_CORE_NAMES` in
`tmplugin/corenames.py`, and to `tmtitle.py`'s `_OWNERS` under `corenames`
if anything reaches it as `tmtitle.<name>`.

## Running the checks

```
python check.py --selftest     # the hygiene scanner can fail (positive controls)
python check.py                # nothing private or proprietary in the tree
python tools/tm_run_all.py     # every self-test; -k <substring> picks a few
```

The suites marked `core` in `tools/tm_run_all.py` need the OpenLobby core
checked out beside this repository (or `OPENLOBBY_SERVICES` pointing at its
`services/`). The board suites need the art baked from your install
(`tools/tm_boardart_bake.py`) and skip until it is. A suite that finds no
`POL_DATA_DIR` falls back to `/data`, which on Windows is the root of the
current drive; point `POL_DATA_DIR` at a scratch folder to keep runs apart.
GitHub Actions runs the same commands on every pull request.

Every suite imports the core's `polcore`, so the core has to be found for
all of them, and the drivers have to be installed
(`pip install "psycopg[binary]" psycopg-pool valkey`). The suites in
`NEEDS_DB` in `tools/tm_run_all.py` each get an empty PostgreSQL database
of their own from `tools/tmpg.py`, which uses Docker or the server
`POL_TEST_DATABASE_URL` names; `tm_store_test` also starts a Valkey, or uses
`POL_TEST_VALKEY_URL`. Without a server those suites report SKIP, and
`POL_TEST_REQUIRE_DB=1` makes that a failure. The board suites (`tm_board`,
`tm_faces`) are among them, because the board takes names and portraits
from the core's account tables; a running board needs `POL_DATABASE_URL` for
the same reason and draws without names when it cannot reach the database.

A new self-test is registered by hand in `tools/tm_run_all.py`. The list is
explicit on purpose: a suite that is not registered does not run.

## Where state lives

Tetra Master keeps no files of its own. Anything that must survive a
restart is in the core's PostgreSQL. A member's collection, save and prize
record, the auction's records and the rank lists are rows of the core's
`blob` table, read and written through `services/tmblob.py` under their old
file names. A read-modify-write goes through `tmblob.locked(name)`, one
transaction holding a lock named after the record, so two containers
updating the same record take turns. The tournament standings, the weekly
champion and the board's Discord bookkeeping are CrystalMaster's own tables
(`tm_*`), through `tmstore.py`. Live state that several containers read
(the room roster, the matches being watched, an accept quorum) goes in
Valkey through the core's `polcore.kv` under `tm:` keys. Nothing durable
goes in Valkey: losing it loses who is seated and what is being played, and
nothing else. A file is only for what the operator edits, such as
`tm_chat_roster.txt`.

A selftest that needs an empty store wraps its work in
`selftest_run._blob_sandbox()`. It runs only on the throwaway database
`tm_run_all.py` made (`TM_TEST_DATABASE=1`) and refuses anywhere else.

A schema change is a new file in `services/tm_migrations/` with the next
number. A shipped migration is never edited. The core's `schema_migrations`
table is keyed by the number alone and shared with the core and the other
titles, so CrystalMaster keeps to 3001-3999 and a table name that starts
with `tm_`; a reused number is silently skipped.

Moving a file into the database comes with an importer in `tmstore.py`
(`python tmstore.py import event_state|champion|board_state ...`). It only
reads its source, runs in one transaction, refuses a table that already
holds rows unless given `--merge`, writes nothing with `--dry-run`, and
changes nothing on a second run. `tools/tm_import_test.py` writes the old
files with the code from `game-split` and checks each import against the
new code. The command is added to `TITLES` in OpenLobby's
`tools/db_import.py` and to its `docs/database.md`.

`live_sessions.py` is the core's. Tetra Master publishes its live-match
count with `live_sessions.write_marker` (`live:tm`); a copy of the module
must not be added here. `.dockerignore` keeps one out of the image and the
build refuses an image whose `live_sessions` is not the core's. A deploy
script asks a running container, for example
`docker compose exec -T authsess python live_sessions.py count tm`.

## What a pull request needs

- One topic per pull request, with a subject line that says what the game
  now does differently ("Tetra Master: a full table refuses a reservation
  with /EN=-32870").
- The checks above green, and a self-test for behaviour that can be pinned
  offline. A change to what a suite pins updates the suite in the same pull
  request.
- No Square Enix content: no client tables, captured server blobs, card art
  or fonts, and no captured packets in tests. Code that reads such data from
  the user's own install is fine.
- Nothing private: no real addresses or hostnames, no member names or ids.
  A new address becomes an environment knob with a loopback or empty default.
- Plain prose in comments and docs: say what the code does and why; leave out
  session narration.
- Behaviour that exists because the client needs it keeps a comment saying
  which client routine reads it and what happens without it. The protocol was
  read off the client, and a reader cannot tell a client quirk from a mistake
  without that note.

## Reporting a bug

Open an issue with the client (Viewer build or console), what you were doing
in the game, the `tm:` log lines around the failure (`docker logs` of the
`authsess` container), and what the client showed.
