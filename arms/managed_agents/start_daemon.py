"""Start core.server detached from the exec call (new session, own log).

Exec runs one command and returns; a plain `cmd &` may be killed with the
exec's process group. start_new_session=True is setsid().

  python3 start_daemon.py <install_dir> <workspace_dir>
Idempotent: if /health answers, does nothing.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

d, ws = sys.argv[1], sys.argv[2]
port = os.environ.get("PORT", "8080")


def healthy():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


h = healthy()
if h:
    print(json.dumps({"already_running": True, **h}))
    sys.exit(0)

os.makedirs(ws, exist_ok=True)
key_file = os.path.join(d, ".model_key")
extra = {}
if not os.environ.get("MODEL_ACCESS_KEY") and os.path.exists(key_file):
    extra["MODEL_ACCESS_KEY"] = open(key_file).read().strip()
env = dict(os.environ, **extra, ARM="managed_agents", HOST="127.0.0.1", PORT=port,
           WORKSPACE=ws, PYTHONUNBUFFERED="1")
log = open(os.path.join(d, "server.log"), "ab")
proc = subprocess.Popen([sys.executable, "-m", "core.server"], cwd=d, env=env,
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        start_new_session=True)
# Local fake harness (and operators) use this pidfile to pause/resume the
# daemon; /proc environ scanning is Linux-only.
pid_path = os.path.join(os.environ.get("HOME", d), ".fake_pids")
with open(pid_path, "a") as pf:
    pf.write(f"{proc.pid}\n")
for _ in range(50):
    time.sleep(0.2)
    h = healthy()
    if h:
        print(json.dumps({"started": True, **h,
                          "model_key_present": bool(env.get("MODEL_ACCESS_KEY")),
                          "model_id": os.environ.get("MODEL_ID", "")}))
        sys.exit(0)
print(json.dumps({"started": False, "log_tail": open(os.path.join(d, "server.log"),
                                                     errors="replace").read()[-2000:]}))
sys.exit(1)
