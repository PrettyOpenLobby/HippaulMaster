"""tmblob.py -- Tetra Master's stored records in the core's blob table.

Tetra Master used to keep its own files under POL_RESOURCE_DIR, beside the
resources the lobby serves: each member's collection, save and prize record,
the auction house's pending credits and bid histories, and the published
ranking lists. The core moved its resource store onto PostgreSQL (the `blob`
table, OpenLobby's polcore/blobs.py), so these records moved with it, or the
lobby would serve a save Tetra Master had never written.

EVERY RECORD KEEPS ITS OLD FILE NAME AS ITS KEY. The part of the name before
the first dot is the blob's scope and the rest its path
(`polcore.blobs.split_name`), exactly as the core names its own resources, so
the core and this module address the same row, and an import of an old
resources/ directory lands every file where it is read:

    <m>.tm_collection.json       scope <m>, path tm_collection.json
    <m>.U_g_TM0DataFile.bin      scope <m>, path U_g_TM0DataFile.bin (the save)
    <m>.tm_prize.json            scope <m>, path tm_prize.json
    auction-pending-<m>.json     scope auction-pending-<m>, path json
    auction-<n>.bids.bin         scope auction-<n>, path bids.bin
    tmrank.<list>.bin            scope tmrank (was the tmrank/ directory)
    tmrank.pool.json             scope tmrank, path pool.json

A write is one statement, so a reader sees the old record or the new one and
never part of either. A read-modify-write goes through `locked(name)`, one
transaction holding an advisory lock named after the record, so two
containers updating the same record take turns instead of losing a write.
"""
import contextlib
import json

import tmstore
from polcore import blobs


def key(name):
    """(scope, path) for a record name. ValueError for a name with no dot."""
    k = blobs.split_name(name)
    if k is None:
        raise ValueError("%r is not a <scope>.<path> record name" % (name,))
    return k


def name_of(info):
    """The record name of a `polcore.blobs.Info`."""
    return blobs.file_name(info.scope, info.path)


def errors():
    """What a read or write raises when the database is not there (or the
    name is not a record name), as a tuple for `except`."""
    return (ValueError,) + tmstore.errors()


def _ready():
    tmstore.ensure_core_schema()


def read(name, conn=None):
    """The stored bytes, or None when there is no such record."""
    _ready()
    return blobs.get(*key(name), conn=conn)


def write(name, data, conn=None):
    """Store `data` (bytes) under `name`, replacing what was there."""
    _ready()
    return blobs.put(*key(name), bytes(data), conn=conn)


def size(name):
    """The stored size in bytes, or None when there is no such record."""
    _ready()
    st = blobs.stat(*key(name))
    return None if st is None else st.size


def mtime(name):
    """When `name` was last written (epoch seconds), or None."""
    _ready()
    st = blobs.stat(*key(name))
    return None if st is None else st.updated_at


def exists(name):
    return size(name) is not None


def delete(name, conn=None):
    """Remove a record. True when there was one."""
    _ready()
    return blobs.delete(*key(name), conn=conn)


def names(scope=None, prefix=None, suffix=None, path=None):
    """The names of the stored records matching every filter
    (`polcore.blobs.listing`), sorted."""
    _ready()
    return sorted(name_of(i) for i in blobs.listing(
        scope=scope, prefix=prefix, suffix=suffix, path=path))


def read_json(name, conn=None):
    """The JSON document stored under `name`, or None when there is none.
    Raises ValueError for a record that is not JSON."""
    raw = read(name, conn=conn)
    if raw is None:
        return None
    return json.loads(raw.decode("utf-8"))


def write_json(name, data, conn=None, **dump):
    """Store `data` as JSON under `name` (`dump` goes to json.dumps)."""
    return write(name, json.dumps(data, **dump).encode("utf-8"), conn=conn)


@contextlib.contextmanager
def locked(name):
    """A connection inside one transaction that holds the advisory lock
    `tmblob:<name>`: read the record with `conn=`, work out the new one,
    write it with `conn=`, and nobody else's update of the same record lands
    in between. Commits at the end of the block, rolls back if it raises."""
    _ready()
    key(name)
    with tmstore.db.transaction(lock="tmblob:" + name) as conn:
        yield conn
