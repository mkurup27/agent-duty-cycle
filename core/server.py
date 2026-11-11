"""Long-lived agent process: used unchanged by arm (a) Droplet and arm (c)
Managed Agents sandbox. State is simply the in-memory agent object plus the
local workspace directory. There is no persistence code here, on purpose.

  python3 -m core.server            # reads config from env

HTTP API
  POST /event?wait=S   enqueue an event; block up to S seconds (default 900)
                       for the result. 200 = result, 202 = still running.
  GET  /result/<id>    200 = result, 202 = pending, 404 = unknown
  GET  /health

Events run one at a time on a single worker thread, like the other arms.
The wait/poll split exists because the Harness Runtime exec API caps a
command at 240 s, and some events run longer than that.

Env:
  ARM          label written into every response (droplet | managed_agents)
  HOST, PORT   bind address (default 127.0.0.1:8080)
  WORKSPACE    workspace dir (default ./workspace)
  AGENT_ID     default agent-0
  AGENT_TOKEN  if set, POST /event requires header X-Agent-Token
  + model env vars (see core/model.py)
"""

import collections
import hmac
import json
import os
import queue
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .agent import OpsTriageAgent
from .model import ModelClient
from .state import AgentState
from .timing import PhaseTimer, PROCESS_START_WALL

ARM = os.environ.get("ARM", "server")
TOKEN = os.environ.get("AGENT_TOKEN", "")
AGENT = OpsTriageAgent(ModelClient(),
                       os.environ.get("WORKSPACE", "./workspace"),
                       AgentState(agent_id=os.environ.get("AGENT_ID", "agent-0")),
                       arm=ARM)

QUEUE = queue.Queue()
RESULTS = collections.OrderedDict()   # event_id -> result (bounded)
DONE = {}                             # event_id -> threading.Event
MAX_RESULTS = 1000
LOCK = threading.Lock()


def worker():
    while True:
        ev, timer = QUEUE.get()
        timer.marks["queue_wait_s"] = round(timer.elapsed(), 4)
        timer.mark("state_loaded")   # state is already in memory: ~0
        out = AGENT.handle(ev, timer)
        timer.mark("state_saved")    # nothing to save
        out["timing"] = timer.to_dict()
        with LOCK:
            RESULTS[ev["id"]] = out
            while len(RESULTS) > MAX_RESULTS:
                old, _ = RESULTS.popitem(last=False)
                DONE.pop(old, None)
            DONE[ev["id"]].set()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return not TOKEN or hmac.compare_digest(
            self.headers.get("X-Agent-Token", ""), TOKEN)

    def _wait_result(self, eid, wait):
        ev = DONE.get(eid)
        if ev is None:
            return self._send(404, {"ok": False, "error": "unknown_event"})
        if ev.wait(timeout=wait):
            return self._send(200, RESULTS[eid])
        return self._send(202, {"pending": True, "event_id": eid})

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "arm": ARM, "pid": os.getpid(),
                                    "process_start": PROCESS_START_WALL,
                                    "queued": QUEUE.qsize(),
                                    "events_processed": AGENT.state.events_processed})
        if self.path.startswith("/result/"):
            if not self._authed():
                return self._send(401, {"ok": False, "error": "unauthorized"})
            return self._wait_result(urllib.parse.unquote(self.path[8:]), 0)
        self._send(404, {"ok": False})

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path != "/event":
            return self._send(404, {"ok": False})
        if not self._authed():
            return self._send(401, {"ok": False, "error": "unauthorized"})
        timer = PhaseTimer()
        n = int(self.headers.get("Content-Length", 0))
        ev = json.loads(self.rfile.read(n) or b"{}")
        ev.setdefault("id", f"anon-{time.time_ns()}")
        wait = float(urllib.parse.parse_qs(u.query).get("wait", ["900"])[0])
        with LOCK:
            known = ev["id"] in DONE
            if not known:
                DONE[ev["id"]] = threading.Event()
        if not known:   # a retried delivery just waits for the original
            QUEUE.put((ev, timer))
        self._wait_result(ev["id"], wait)


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer((host, port), Handler)
    print(json.dumps({"listening": f"{host}:{port}", "arm": ARM}), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
