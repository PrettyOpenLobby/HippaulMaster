# Contributing to CrystalMaster

CrystalMaster is the Tetra Master title for the OpenLobby core. It runs inside
the core's `login` and `authsess` processes as a plugin, so most of what a
client does in Tetra Master arrives here as one line of the game channel and
leaves as the line that answers it. This page says where things are, how to
run the checks, and what a pull request needs.

## Where things are

```
services/
  tmtitle.py        the plugin the core loads (POL_TITLES=tmtitle): the seam
                    that hands the core's traffic to the game, plus the room
                    roster, auction, rankings, trade relay and lobby counts
  tetramaster.py    the game server's public name and a facade over tmgame/;
                    see below
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
tools/split/        the generator that cut tetramaster.py into tmgame/
config/polpro.json  reply templates for the POLpro plaintext channel
```

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

`deps.py` holds the imports the modules share, including the optional
siblings (`tmauction`, `tmsave`, `tmprize`, `tmroll`) that are `None` when
missing. `common.py` holds `_say`, the log line every module uses.

### The facade

`services/tetramaster.py` is where the whole game server used to live. It is
now a generated module that imports `tmgame` and forwards
`tetramaster.<name>` reads and writes to the module that owns the name.
`tmtitle`, `tmtables`, the tools and the tests keep using `import
tetramaster`, including the tests that rebind a name to quiet or fake a
helper (`tm._say = lambda *a, **k: None`): the write lands in the owning
module, so the code under test sees it. `tools/facade_rebind_check.py` proves
the forwarding holds for every rebinding the tools make. New code inside
`tmgame/` refers to a sibling as `<module>.<name>`.

### Regenerating the split

The package is the output of `tools/split/split_tetramaster.py` over the flat
file and `tools/split/split_tetramaster_map.txt`, which names the module each
top-level function, class and global belongs to. Code ported from elsewhere
as a change to the flat file is split again the same way:

```
git show <split commit>^:services/tetramaster.py > flat.py   # then merge into flat.py
python tools/split/split_tetramaster.py --src flat.py \
    --map tools/split/split_tetramaster_map.txt \
    --out services/tmgame --facade services/tetramaster.py
```

A new top-level name needs a line in the map; the tool lists anything
unmapped and refuses to write until it is placed. It also refuses a module
name that a function in the package uses as a local variable.

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
