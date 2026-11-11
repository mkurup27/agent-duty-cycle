"""Hand one event to the in-sandbox agent daemon (arm c).

Invoked through the Harness Runtime exec API (argv runs with no shell):
  submit:  argv = ["python3", ".../core/deliver.py", "submit", "<wait_s>"]
           stdin = JSON event
  result:  argv = ["python3", ".../core/deliver.py", "result", "<event_id>"]

Prints JSON on stdout. {"pending": true} means "still running, poll again".
Exec commands are capped at 240 s, so callers keep wait_s well under that.

Stdlib only, and imports nothing from the rest of core, so it starts fast.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
HDRS = {"Content-Type": "application/json",
        "X-Agent-Token": os.environ.get("AGENT_TOKEN", "")}


def _call(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}") | {"http_status": e.code}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "daemon_unreachable", "detail": str(e)}


def main():
    t0 = time.perf_counter()
    mode = sys.argv[1] if len(sys.argv) > 1 else "submit"
    if mode == "result":
        eid = urllib.parse.quote(sys.argv[2], safe="")
        out = _call(urllib.request.Request(f"{BASE}/result/{eid}", headers=HDRS), 30)
    else:
        wait = float(sys.argv[2]) if len(sys.argv) > 2 else 200
        out = _call(urllib.request.Request(
            f"{BASE}/event?wait={wait}", data=sys.stdin.read().encode(),
            headers=HDRS), wait + 30)
    out["deliver_s"] = round(time.perf_counter() - t0, 4)
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
