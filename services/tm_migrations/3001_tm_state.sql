-- Tetra Master's durable state, formerly JSON files under /data.
--
-- CrystalMaster's migrations are numbered 3001..3999 because they share
-- OpenLobby's schema_migrations table, which is keyed by version alone.
-- Every table here starts with tm_. None has a foreign key into the account
-- tables: a member id is kept as the text the game already used for it, and
-- a row outliving a deleted member is what the files did too.

-- tmeventstate.py (formerly tm-event-state.json): one row per player per
-- tournament window. `event_window` is the window's start (unix seconds),
-- `member` the member id as text, `data` the player's row as the file held it
-- (steps, games, wins, perfect, ties, streak, best_streak, missions, deck,
-- paid, at, and the mission counters).
CREATE TABLE tm_event_standing (
    event_window BIGINT NOT NULL,
    member       TEXT   NOT NULL,
    data         JSONB  NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_window, member)
);

-- The weekly champion (formerly tm-champion.json): last week's #1 by VS.
-- Rating, named by the Sunday publish (tools/tmrank.py) and read by the card
-- shop (tmgame/shopdoors.py) for the rank_1 Pack's label. One row per week,
-- so the history stays; the newest row is the current champion.
CREATE TABLE tm_champion (
    week      INTEGER PRIMARY KEY,
    member_id TEXT NOT NULL,
    name      TEXT NOT NULL,
    named_at  DOUBLE PRECISION NOT NULL
);
CREATE INDEX tm_champion_named_at ON tm_champion (named_at);

-- The web board's Discord bookkeeping (polboards.py, formerly
-- /state/<name>_discord.json and /state/discord_channels.json): which
-- messages the board posted and edits, and where each feed posts. `name` is
-- what the file was called without its extension.
CREATE TABLE tm_board_state (
    name       TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
