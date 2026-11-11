"""OpenAI-compatible chat client for DO Serverless Inference.

Uses one persistent HTTP(S) connection per process on purpose: whether that
connection survives between events is one of the liveness signals the
experiment reports (always-on: yes; stateless: never; pause/resume: we find
out). Stdlib only, so it deploys unchanged to a Droplet, a Function and a
sandbox.

Env:
  MODEL_BASE_URL   default https://inference.do-ai.run/v1
  MODEL_ID         model slug from GET {MODEL_BASE_URL}/models
  MODEL_ACCESS_KEY DO model access key
"""

import http.client
import json
import os
import time
import urllib.parse


class ModelError(RuntimeError):
    pass


class ModelClient:
    def __init__(self, base_url=None, model_id=None, api_key=None, timeout=120):
        self.base_url = (base_url or os.environ.get("MODEL_BASE_URL")
                         or "https://inference.do-ai.run/v1").rstrip("/")
        self.model_id = model_id or os.environ.get("MODEL_ID", "")
        self.api_key = api_key or os.environ.get("MODEL_ACCESS_KEY", "")
        self.timeout = timeout
        u = urllib.parse.urlsplit(self.base_url)
        self._scheme, self._host, self._path = u.scheme, u.netloc, u.path
        self._conn = None
        self.conn_opened = 0     # connections opened by this process
        self.conn_requests = 0   # requests sent over the current connection
        self.totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                       "cached_input_tokens": 0}

    # -- connection management -------------------------------------------
    # Evict a kept-alive connection that has sat idle longer than this. Found
    # in the week run (BUGLOG B-007 / P-005): after a sandbox pause the socket
    # still looks open but is dead, and the first request waited the full
    # read timeout (120 s) before reconnecting. Idle eviction is ordinary
    # HTTP-client practice and runs identically on every arm.
    IDLE_EVICT_S = float(os.environ.get("MODEL_IDLE_EVICT_S", "30"))
    # Retry transient provider errors (Serverless Inference returned
    # 529 "Platform overloaded" on ~2% of calls in the first week-run hours).
    RETRY_STATUS = (429, 500, 502, 503, 504, 529)
    BACKOFF_S = (2, 4, 8, 16)

    def _connect(self):
        cls = (http.client.HTTPSConnection if self._scheme == "https"
               else http.client.HTTPConnection)
        self._conn = cls(self._host, timeout=self.timeout)
        self.conn_opened += 1
        self.conn_requests = 0

    def liveness(self):
        return {"conn_open": self._conn is not None,
                "conn_opened_total": self.conn_opened,
                "conn_requests_on_current": self.conn_requests}

    def _send_once(self, path, payload, headers, meta):
        """One request with one reconnect on a transport error."""
        for attempt in (1, 2):
            if self._conn is None:
                self._connect()
            try:
                self._conn.request("POST", self._path + path, payload, headers)
                resp = self._conn.getresponse()
                data = resp.read()
                self.conn_requests += 1
                self._last_used = time.monotonic()
                return resp.status, data
            except (http.client.HTTPException, OSError) as e:
                self._conn.close()
                self._conn = None
                meta["reused"] = False
                meta["transport_errors"] = meta.get("transport_errors", 0) + 1
                if attempt == 2:
                    raise ModelError(f"connection failed: {e}") from e

    def _post(self, path, body):
        payload = json.dumps(body).encode()
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        meta = {"retries": 0, "retry_wait_s": 0.0, "evicted_idle": False}
        if self._conn is not None:
            idle = time.monotonic() - getattr(self, "_last_used", time.monotonic())
            meta["idle_s"] = round(idle, 1)
            if idle > self.IDLE_EVICT_S:
                self._conn.close()
                self._conn = None
                meta["evicted_idle"] = True
        meta["reused"] = self._conn is not None
        for i in range(len(self.BACKOFF_S) + 1):
            status, data = self._send_once(path, payload, headers, meta)
            if status < 400:
                return json.loads(data), meta
            if status in self.RETRY_STATUS and i < len(self.BACKOFF_S):
                meta["retries"] += 1
                meta["last_retry_status"] = status
                time.sleep(self.BACKOFF_S[i])
                meta["retry_wait_s"] += self.BACKOFF_S[i]
                continue
            raise ModelError(f"HTTP {status}: {data[:300]!r}")

    # -- API ---------------------------------------------------------------
    def chat(self, messages, max_tokens=None, temperature=0.0):
        # temperature 0: replies (and so transcript length and token counts)
        # stay as close to identical across arms as the provider allows.
        t = time.perf_counter()
        body = {"model": self.model_id, "messages": messages,
                "max_tokens": max_tokens or int(os.environ.get("MODEL_MAX_TOKENS", "256")),
                "temperature": temperature}
        # MODEL_EXTRA_BODY: JSON merged into every request, e.g. a provider's
        # switch for turning reasoning off. Identical on every arm.
        extra = os.environ.get("MODEL_EXTRA_BODY")
        if extra:
            body.update(json.loads(extra))
        data, meta = self._post("/chat/completions", body)
        usage = data.get("usage") or {}
        pdet = usage.get("prompt_tokens_details") or {}
        cdet = usage.get("completion_tokens_details") or {}
        rec = {
            "latency_s": round(time.perf_counter() - t, 4),
            "input_tokens": int(usage.get("prompt_tokens", 0)),
            "output_tokens": int(usage.get("completion_tokens", 0)),
            # DO reports cache reads as cache_read_input_tokens; OpenAI-style
            # APIs use prompt_tokens_details.cached_tokens. Accept either.
            "cached_input_tokens": int(usage.get("cache_read_input_tokens")
                                       or pdet.get("cached_tokens") or 0),
            "reasoning_tokens": int(cdet.get("reasoning_tokens") or 0),
            "conn_reused": meta["reused"],
            "conn_evicted_idle": meta["evicted_idle"],
            "conn_idle_s": meta.get("idle_s"),
            "retries": meta["retries"],
            "retry_wait_s": meta["retry_wait_s"],
            "transport_errors": meta.get("transport_errors", 0),
        }
        self.totals["calls"] += 1
        for k in ("input_tokens", "output_tokens", "cached_input_tokens"):
            self.totals[k] += rec[k]
        msg = data["choices"][0]["message"]
        text = msg.get("content") or ""
        rec["empty_content"] = not text
        return text, rec
