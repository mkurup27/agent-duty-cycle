"""Wipe ALL stateless-arm state (every agent_state row + its Spaces snapshot).

  python3 tools/reset_b.py            # dry run: lists what would go
  python3 tools/reset_b.py --yes      # delete

Reads DATABASE_URL / SPACES_* from ~/fn.env. Run from the repo root.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for line in open(os.path.expanduser("~/fn.env")):
    k, sep, v = line.strip().partition("=")
    if sep and k and not k.startswith("#"):
        os.environ[k] = v
sys.path[:0] = [os.path.join(ROOT, "arms/functions/packages/agent/handle"), ROOT]
from db import Db  # noqa: E402
from s3mini import Spaces  # noqa: E402

db, sp = Db(), Spaces()
cur = db.conn.cursor()
cur.execute("SELECT agent_id, ws_key FROM agent_state")
rows = cur.fetchall()
cur.close()
print(f"{len(rows)} rows:", [r[0] for r in rows])
if "--yes" not in sys.argv:
    sys.exit("dry run - add --yes to delete")
for aid, key in rows:
    if key:
        print("blob", key, sp.delete(key))
    db.execute("DELETE FROM agent_state WHERE agent_id = %s", (aid,))
print("rows left:", db.fetchone("SELECT count(*) FROM agent_state")[0])
