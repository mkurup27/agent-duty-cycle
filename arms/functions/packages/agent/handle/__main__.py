"""Arm (b): DO Functions action. One invocation = one event.

Every invocation rehydrates state from Postgres + Spaces, runs the same core
agent as the other arms, and writes state back. Process-level globals (DB
connection, model client, helper subprocess) are reused when the platform
happens to give us a warm container - standard practice - and the response
reports whether that happened, so the data shows how often you can rely on it.

Env (set in project.yml from .env):
  DATABASE_URL, PG_CA_CERT, SPACES_*, MODEL_*,
  BLOB_BACKEND=spaces|local (local only for tests), LOCAL_BLOB_DIR
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.agent import OpsTriageAgent  # noqa: E402
from core.model import ModelClient  # noqa: E402
from core.timing import PhaseTimer  # noqa: E402
from db import Db  # noqa: E402
from state_store import Conflict, StateStore  # noqa: E402

_G = {}  # warm-container cache: db, model, helper


def _blobs():
    if os.environ.get("BLOB_BACKEND", "spaces") == "local":
        from local_blobs import LocalBlobs  # test-only stand-in
        return LocalBlobs(os.environ["LOCAL_BLOB_DIR"])
    from s3mini import Spaces
    return Spaces()


def _resources(phases):
    t = time.perf_counter()
    warm = "db" in _G
    if not warm or not _G["db"].ping():
        _G["db"] = Db()
        _G["db"].ensure_schema()
        _G["db_connects"] = _G.get("db_connects", 0) + 1
    _G.setdefault("model", ModelClient())
    _G.setdefault("blobs", _blobs())
    phases["resources_s"] = round(time.perf_counter() - t, 4)
    phases["warm_container"] = warm
    return _G["db"], _G["model"], _G["blobs"]


def handle_event(ev):
    timer = PhaseTimer()
    phases = {}
    db, model, blobs = _resources(phases)
    store = StateStore(db, blobs, os.environ.get("TMP_ROOT", "/tmp/dcagent"))
    agent_id = ev.get("agent_id") or os.environ.get("AGENT_ID", "agent-0")
    st, ws, meta = store.load(agent_id, phases)
    timer.mark("state_loaded")
    agent = OpsTriageAgent(model, ws, st, arm="functions")
    agent.helper = _G.get("helper")       # survives only in a warm container
    out = agent.handle(ev, timer)
    _G["helper"] = agent.helper
    if out.get("ok") and not out.get("duplicate_delivery"):
        try:
            out["state_version"] = store.save(agent.state, ws, meta, phases)
        except Conflict as e:
            out.update(ok=False, error="conflict", detail=str(e))
    timer.mark("state_saved")
    out["state_io"] = phases
    out["timing"] = timer.to_dict()
    return out


def main(args):
    # Invoked through the authenticated Functions REST API (namespace access
    # key), not as a web function: web functions cannot run longer than the
    # 30 s blocking window, and our events run up to several minutes.
    ev = {k: v for k, v in args.items() if not k.startswith("__ow_")}
    return {"body": handle_event(ev)}
