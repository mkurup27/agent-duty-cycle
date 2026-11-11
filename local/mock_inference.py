"""Local stand-in for DO Serverless Inference (OpenAI-compatible).

  python3 local/mock_inference.py --port 9100 --latency 0.05

Counts tokens as chars/4 so token totals in tests are deterministic.
"""

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LATENCY = 0.05
FAIL_FIRST = 0      # answer the first N requests with HTTP 529 (retry tests)
SEEN = [0]


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "mock-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n))
        SEEN[0] += 1
        if SEEN[0] <= FAIL_FIRST:
            b = b'{"error":{"type":"overloaded_error"}}'
            self.send_response(529)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        prompt_chars = sum(len(m.get("content", "")) for m in req["messages"])
        time.sleep(LATENCY)
        text = "1. Check logs. 2. Roll back last deploy. 3. Verify health. OK"
        body = json.dumps({
            "id": "mock", "object": "chat.completion", "model": req.get("model"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": max(1, prompt_chars // 4),
                      "completion_tokens": len(text) // 4,
                      "total_tokens": prompt_chars // 4 + len(text) // 4},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    global LATENCY, FAIL_FIRST
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--latency", type=float, default=0.05)
    ap.add_argument("--fail-first", type=int, default=0)
    a = ap.parse_args()
    LATENCY, FAIL_FIRST = a.latency, a.fail_first
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
