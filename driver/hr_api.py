"""Thin client for the DO Harness Runtime API (/v2/agents/sessions...).

Endpoints used (API reference, Oct 2026):
  POST   /v2/agents/sessions                 create (YAML env spec body)
  GET    /v2/agents/sessions/{id}            status: SESSION_STATUS_*
  POST   /v2/agents/sessions/{id}/pause      204
  POST   /v2/agents/sessions/{id}/resume     204, returns once sandbox ready
  POST   /v2/agents/sessions/{id}/sandbox/exec   {argv, stdin(b64), timeout_seconds<=240}
  DELETE /v2/agents/sessions/{id}

Env: DIGITALOCEAN_TOKEN, DO_API_BASE (default https://api.digitalocean.com;
the local fake uses http://127.0.0.1:PORT).
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request


class HRError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class HarnessRuntime:
    def __init__(self, token=None, base=None):
        self.token = token or os.environ.get("DIGITALOCEAN_TOKEN", "")
        self.base = (base or os.environ.get("DO_API_BASE")
                     or "https://api.digitalocean.com").rstrip("/")
        self.retries = []

    def _req(self, method, path, body=None, ctype="application/json", timeout=300):
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raise HRError(e.code, e.read().decode(errors="replace")) from e

    # -- sessions ---------------------------------------------------------
    def create_from_yaml(self, yaml_text, attempts=4):
        """Create a session, retrying server-side (5xx) failures.

        Seen in practice (2026-10-27): HTTP 500 "credential storage request
        failed" on an otherwise valid spec. Before each retry we look the
        name up, so a create that actually succeeded is never duplicated.
        Every retry is recorded in self.retries for the run log.
        """
        name = None
        for line in yaml_text.splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip().strip('"')
        for i in range(attempts):
            try:
                _, out = self._req("POST", "/v2/agents/sessions", yaml_text.encode(),
                                   ctype="application/yaml")
                return out.get("session", out)
            except HRError as e:
                if e.status < 500 or i == attempts - 1:
                    raise
                self.retries.append({"op": "create", "name": name, "status": e.status,
                                     "body": e.body[:300], "t": time.time()})
                time.sleep(5 * 2 ** i)
                existing = self.find_by_name(name) if name else None
                if existing:
                    return existing

    def list_configs(self, max_pages=50):
        """All Environment Configs (paginated via next_page_token)."""
        out, tok = [], None
        for _ in range(max_pages):
            path = "/v2/agents/configs" + (f"?page_token={tok}" if tok else "")
            page = self._req("GET", path)[1]
            out += page.get("configs", [])
            tok = page.get("next_page_token")
            if not tok:
                break
        return out

    def delete_config(self, config_id):
        self._req("DELETE", f"/v2/agents/configs/{config_id}")

    def get(self, sid):
        _, out = self._req("GET", f"/v2/agents/sessions/{sid}")
        return out.get("session", out)

    def find_by_name(self, name):
        _, out = self._req("GET", f"/v2/agents/sessions?name={name}")
        for s in out.get("sessions", []):
            if s.get("name") == name:
                return s
        return None

    def status(self, sid):
        return self.get(sid).get("status", "")

    def pause(self, sid):
        self._req("POST", f"/v2/agents/sessions/{sid}/pause")

    def resume(self, sid):
        self._req("POST", f"/v2/agents/sessions/{sid}/resume")

    def delete(self, sid):
        self._req("DELETE", f"/v2/agents/sessions/{sid}")

    def exec(self, sid, argv, stdin=b"", timeout_s=240, workdir=None):
        body = {"argv": argv, "timeout_seconds": timeout_s}
        if stdin:
            body["stdin"] = base64.b64encode(stdin).decode()
        if workdir:
            body["workdir"] = workdir
        _, out = self._req("POST", f"/v2/agents/sessions/{sid}/sandbox/exec",
                           body, timeout=timeout_s + 60)
        return out

    def wait_status(self, sid, want=("SESSION_STATUS_READY",), timeout=600, every=2):
        t = time.time()
        while time.time() - t < timeout:
            st = self.status(sid)
            if st in want:
                return st
            if st in ("SESSION_STATUS_FAILED", "SESSION_STATUS_DESTROYED"):
                raise HRError(0, f"session {sid} is {st}")
            time.sleep(every)
        raise TimeoutError(f"{sid} not in {want} after {timeout}s")
