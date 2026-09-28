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
  boardtm.py        the optional live board (rankings, auction, matches)
  polboards.py      the board service and its Discord posting
  polgateway.py     the Discord Gateway presence for the board bot
  tmdata/           the client's parameter tables, decoded from YOUR install
                    by tools/tmdata_build.py (only card_names_en.txt ships)
  boardart/         the board's fonts; its art is baked from your install
tools/              self-tests (`*_test.py`), the runner (tm_run_all.py) and
                    operator tools (tmrank_job.py, tm_money_reset.py, ...)
tools/split/        the generator that cut tetramaster.py and tmtitle.py into
                    their packages, and the two name maps
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
`services/tmtitle.py` the whole plugin. Each is now a generated module that
imports its package and forwards `<module>.<name>` reads and writes to the
module that owns the name. The core (`POL_TITLES=tmtitle`), `tmtables`, the
tools and the tests keep using `import tetramaster` and `import tmtitle`,
including the tests that rebind a name to quiet or fake a helper
(`tm._say = lambda *a, **k: None`, `R._live_rooms = lambda: live`): the write
lands in the owning module, so the code under test sees it.
`tools/facade_rebind_check.py` proves the forwarding holds for every
rebinding the tools make. New code inside a package refers to a sibling as
`<module>.<name>`.

### Regenerating the split

Each package is the output of `tools/split/split_tetramaster.py` over the
flat file and a name map (`tools/split/split_tetramaster_map.txt`,
`tools/split/split_tmtitle_map.txt`), which names the module each top-level
function, class and global belongs to. Code ported from elsewhere as a change
to a flat file is split again the same way, starting from the flat file as it
was in the last commit before the split:

```
git show <last flat commit>:services/tetramaster.py > flat.py   # then merge into flat.py
python tools/split/split_tetramaster.py --src flat.py \
    --map tools/split/split_tetramaster_map.txt \
    --out services/tmgame --facade services/tetramaster.py

git show <last flat commit>:services/tmtitle.py > flat_title.py   # likewise
python tools/split/split_tetramaster.py --src flat_title.py \
    --map tools/split/split_tmtitle_map.txt \
    --out services/tmplugin --facade services/tmtitle.py \
    --summary "Tetra Master as a title plugin for the OpenLobby core." \
    --package-summary "The Tetra Master title plugin, one module per concern." \
    --used-by '`POL_TITLES={facade}` in the core, and the tools and tests' \
    --example '`R._live_rooms = lambda: live`' --doc-heading "The plugin"
```

A new top-level name needs a line in the map; the tool lists anything
unmapped and refuses to write until it is placed. It also refuses a module
name that a function in the package uses as a local variable. A core name the
plugin starts to use is added to `_CORE_NAMES` and to the `[corenames]` list
in the map.

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

A new self-test is registered by hand in `tools/tm_run_all.py`. The list is
explicit on purpose: a suite that is not registered does not run.

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
