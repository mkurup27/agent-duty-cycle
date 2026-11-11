"""State externalization for the stateless arm. THIS FILE IS THE TAX.

Everything between "# TAX:begin <category>" and "# TAX:end" markers is
counted by tools/count_tax.py. Categories:
  serialize    turning live state into bytes and back
  persist      reading/writing Postgres + Spaces
  consistency  versioning, conflict detection, schema migration, cleanup

Layout:
  Postgres  agent_state(agent_id PK, version, schema_version, state_json,
                        ws_key, ws_sig, ws_bytes, updated_at)
  Spaces    agents/<agent_id>/ws-<version>.tar   (workspace snapshot)

Every wake: SELECT row -> GET tar -> extract -> run agent -> if workspace
changed: tar -> PUT new key -> UPDATE row (version CAS) -> DELETE old key.
"""

import hashlib
import json
import os
import shutil
import tarfile
import time
from pathlib import Path

# core is vendored next to this file at deploy time (see deploy.sh)
from core.state import AgentState

# TAX:begin consistency
SCHEMA_VERSION = 1


class Conflict(Exception):
    """Another invocation committed a newer version first."""


def _migrate(doc, from_version):
    # v1 is the first schema. Every future change to AgentState needs a step
    # here, or old rows fail to load. Always-on/pause-resume never need this.
    if from_version == SCHEMA_VERSION:
        return doc
    raise ValueError(f"no migration from schema {from_version}")
# TAX:end


# TAX:begin serialize
def state_to_json(st):
    return json.dumps({
        "agent_id": st.agent_id, "history": st.history,
        "incidents": st.incidents, "feed_cursor": st.feed_cursor,
        "events_processed": st.events_processed, "counts": st.counts,
        "processed_ids": st.processed_ids, "seed": st.seed,
    }, separators=(",", ":"))


def state_from_json(s, schema_version):
    d = _migrate(json.loads(s), schema_version)
    return AgentState(
        agent_id=d["agent_id"], history=d["history"], incidents=d["incidents"],
        feed_cursor=d["feed_cursor"], events_processed=d["events_processed"],
        counts=d["counts"], processed_ids=d["processed_ids"], seed=d["seed"])


def workspace_signature(ws):
    """Cheap change detector: (path, size, mtime_ns) of every file."""
    items = []
    for p in sorted(Path(ws).rglob("*")):
        if p.is_file():
            s = p.stat()
            items.append(f"{p.relative_to(ws)}:{s.st_size}:{s.st_mtime_ns}")
    # hashlib, not hash(): str hashes are salted per process, so a signature
    # stored by one container would never match one computed by the next.
    # (Caught in review before first run; logged in BUGLOG.md as B-001.)
    return hashlib.sha1("|".join(items).encode()).hexdigest() if items else "empty"


def pack_workspace(ws, tar_path):
    # Uncompressed: workspace data (e.g. seed blobs) is mostly incompressible
    # and gzip would add CPU time to every save.
    with tarfile.open(tar_path, "w") as t:
        t.add(ws, arcname=".")
    return os.path.getsize(tar_path)


def unpack_workspace(tar_path, ws):
    with tarfile.open(tar_path, "r") as t:
        t.extractall(ws)  # trusted: we wrote it
# TAX:end


class StateStore:
    def __init__(self, db, blobs, tmp_root="/tmp/dcagent"):
        self.db = db          # see db.py
        self.blobs = blobs    # s3mini.Spaces or a local stand-in
        self.tmp_root = Path(tmp_root)

    # TAX:begin persist
    def load(self, agent_id, phases):
        """Returns (state, workspace_dir, meta). meta is passed back to save()."""
        t = time.perf_counter()
        row = self.db.fetchone(
            "SELECT version, schema_version, state_json, ws_key, ws_sig, ws_bytes "
            "FROM agent_state WHERE agent_id = %s", (agent_id,))
        phases["db_read_s"] = round(time.perf_counter() - t, 4)
        ws = self.tmp_root / agent_id / "ws"
        # Never trust a warm container's leftovers: another container may have
        # committed newer state since. Wipe and rehydrate every time.
        shutil.rmtree(ws, ignore_errors=True)
        ws.mkdir(parents=True)
        if row is None:
            phases.update(blob_get_s=0.0, extract_s=0.0, bytes_down=0)
            return (AgentState(agent_id=agent_id), ws,
                    {"version": 0, "ws_key": None, "ws_sig": "empty"})
        version, schema_v, state_json, ws_key, ws_sig, ws_bytes = row
        t = time.perf_counter()
        st = state_from_json(state_json, schema_v)
        phases["deserialize_s"] = round(time.perf_counter() - t, 4)
        n = 0
        if ws_key:
            tar = self.tmp_root / agent_id / "in.tar"
            t = time.perf_counter()
            n = self.blobs.get_to_file(ws_key, str(tar))
            phases["blob_get_s"] = round(time.perf_counter() - t, 4)
            t = time.perf_counter()
            unpack_workspace(tar, ws)
            tar.unlink()
            phases["extract_s"] = round(time.perf_counter() - t, 4)
        phases["bytes_down"] = n
        return st, ws, {"version": version, "ws_key": ws_key,
                        "ws_sig": workspace_signature(ws)}

    def save(self, st, ws, meta, phases):
        t = time.perf_counter()
        state_json = state_to_json(st)
        phases["serialize_s"] = round(time.perf_counter() - t, 4)
        new_version = meta["version"] + 1
        sig = workspace_signature(ws)
        ws_key, n = meta["ws_key"], 0
        if sig != meta["ws_sig"]:
            # Versioned key: never overwrite the object the committed row
            # points at, or a crash mid-PUT corrupts the only copy.
            tar = self.tmp_root / st.agent_id / "out.tar"
            t = time.perf_counter()
            pack_workspace(ws, tar)
            phases["pack_s"] = round(time.perf_counter() - t, 4)
            ws_key = f"agents/{st.agent_id}/ws-{new_version}.tar"
            t = time.perf_counter()
            n = self.blobs.put_file(ws_key, str(tar))
            phases["blob_put_s"] = round(time.perf_counter() - t, 4)
            tar.unlink()
        phases["bytes_up"] = n
        t = time.perf_counter()
        self._commit(st.agent_id, meta["version"], new_version, state_json,
                     ws_key, sig, n)
        phases["db_write_s"] = round(time.perf_counter() - t, 4)
        if ws_key != meta["ws_key"] and meta["ws_key"]:
            # Best effort. A crash right here leaks one object (known issue,
            # see BUGLOG.md) - a GC sweep would be yet more tax.
            try:
                self.blobs.delete(meta["ws_key"])
            except Exception:  # noqa: BLE001
                phases["gc_failed"] = True
        return new_version
    # TAX:end

    # TAX:begin consistency
    def _commit(self, agent_id, old_version, new_version, state_json, ws_key,
                sig, nbytes):
        if old_version == 0:
            try:
                self.db.execute(
                    "INSERT INTO agent_state (agent_id, version, schema_version, "
                    "state_json, ws_key, ws_sig, ws_bytes, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (agent_id, new_version, SCHEMA_VERSION, state_json, ws_key,
                     sig, nbytes, time.time()))
            except self.db.IntegrityError as e:
                raise Conflict("row created concurrently") from e
            return
        n = self.db.execute(
            "UPDATE agent_state SET version = %s, schema_version = %s, "
            "state_json = %s, ws_key = %s, ws_sig = %s, ws_bytes = %s, "
            "updated_at = %s WHERE agent_id = %s AND version = %s",
            (new_version, SCHEMA_VERSION, state_json, ws_key, sig, nbytes,
             time.time(), agent_id, old_version))
        if n != 1:
            raise Conflict(f"version {old_version} is stale")
    # TAX:end
