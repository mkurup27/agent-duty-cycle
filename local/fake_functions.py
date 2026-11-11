"""Local stand-in for the DO Functions REST API, for tests only.

Implements just what driver/targets.py uses:
  POST /api/v1/namespaces/{ns}/actions/{pkg}/{action}?blocking=true
  GET  /api/v1/namespaces/{ns}/activations/{id}

Container model (approximates OpenWhisk):
  * each action has at most one warm container: a worker subprocess that
    imports the handler once and serves invocations over stdin/stdout
  * a container idle for --keepalive seconds is killed (next call is cold)
  * blocking calls running past --blocking-window return an activation id

Handler code runs from arms/functions/packages/agent/handle with the repo
root on PYTHONPATH (so `core` imports), SQLite instead of Postgres and a
directory instead of Spaces.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLE = ROOT / "arms/functions/packages/agent/handle"

WORKER_SRC = r"""
import json, sys, time, traceback
sys.path.insert(0, sys.argv[1])
t = time.perf_counter()
import importlib.util
spec = importlib.util.spec_from_file_location("fnmain", sys.argv[1] + "/__main__.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
init_ms = (time.perf_counter() - t) * 1000
sys.stdout.write(json.dumps({"ready": True, "init_ms": init_ms}) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    args = json.loads(line)
    try:
        res = mod.main(args); ok = True
    except Exception:
        res = {"error": traceback.format_exc()}; ok = False
    sys.stdout.write(json.dumps({"ok": ok, "result": res}) + "\n"); sys.stdout.flush()
"""

ACTIVATIONS = {}
CONTAINERS = {}       # action -> {proc, last_used, lock}
CLOCK = threading.Lock()
CFG = {}


def container(action):
    """Return (container, init_ms or None if warm)."""
    with CLOCK:
        c = CONTAINERS.get(action)
        if c and c["proc"].poll() is None:
            return c, None
        env = dict(os.environ, PYTHONPATH=str(ROOT), **CFG["env"],
                   AGENT_ID=CFG["agent_ids"].get(action, action),
                   TMP_ROOT=str(Path(CFG["tmp"]) / action.replace("/", "_")))
        p = subprocess.Popen([sys.executable, "-c", WORKER_SRC, str(HANDLE)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             env=env, text=True, bufsize=1)
        ready = json.loads(p.stdout.readline())
        c = {"proc": p, "last_used": time.time(), "lock": threading.Lock()}
        CONTAINERS[action] = c
        return c, ready["init_ms"]


def reaper():
    while True:
        time.sleep(0.5)
        with CLOCK:
            for a, c in list(CONTAINERS.items()):
                if (not c["lock"].locked() and
                        time.time() - c["last_used"] > CFG["keepalive"]):
                    c["proc"].kill()
                    del CONTAINERS[a]


def invoke(action, args, aid):
    arrived = time.time()
    c, init_ms = container(action)
    with c["lock"]:
        start = time.time()
        c["proc"].stdin.write(json.dumps(args) + "\n")
        c["proc"].stdin.flush()
        out = json.loads(c["proc"].stdout.readline())
        end = time.time()
        c["last_used"] = end
    ann = [{"key": "waitTime", "value": int((start - arrived) * 1000)},
           {"key": "limits", "value": {"memory": 512, "timeout": 900000}}]
    if init_ms is not None:
        ann.append({"key": "initTime", "value": int(init_ms)})
    ACTIVATIONS[aid] = {
        "activationId": aid, "name": action, "start": int(start * 1000),
        "end": int(end * 1000), "duration": int((end - start) * 1000),
        "annotations": ann,
        "response": {"status": "success" if out["ok"] else "application error",
                     "success": out["ok"], "result": out["result"]}}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        parts = self.path.split("?")[0].strip("/").split("/")
        # api v1 namespaces {ns} actions {pkg} {action}
        action = "/".join(parts[5:])
        n = int(self.headers.get("Content-Length", 0))
        args = json.loads(self.rfile.read(n) or b"{}")
        aid = uuid.uuid4().hex
        th = threading.Thread(target=invoke, args=(action, args, aid), daemon=True)
        th.start()
        blocking = "blocking=false" not in self.path
        th.join(CFG["blocking_window"] if blocking else 0)
        if aid in ACTIVATIONS:
            return self._send(200, ACTIVATIONS[aid])
        self._send(202, {"activationId": aid})

    def do_GET(self):
        aid = self.path.rstrip("/").split("/")[-1]
        if aid in ACTIVATIONS:
            return self._send(200, ACTIVATIONS[aid])
        self._send(404, {"error": "The requested activation record does not exist."})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9200)
    ap.add_argument("--keepalive", type=float, default=10)
    ap.add_argument("--blocking-window", type=float, default=30)
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--model-url", default="http://127.0.0.1:9100/v1")
    a = ap.parse_args()
    sd = Path(a.state_dir)
    (sd / "blobs").mkdir(parents=True, exist_ok=True)
    CFG.update(keepalive=a.keepalive, blocking_window=a.blocking_window,
               tmp=str(sd / "tmp"),
               env={"DATABASE_URL": f"sqlite:///{sd / 'agent.db'}",
                    "BLOB_BACKEND": "local", "LOCAL_BLOB_DIR": str(sd / "blobs"),
                    "MODEL_BASE_URL": a.model_url, "MODEL_ID": "mock",
                    "MODEL_ACCESS_KEY": "x"},
               agent_ids={})
    threading.Thread(target=reaper, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
