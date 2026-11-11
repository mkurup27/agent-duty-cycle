"""Create, install, smoke-test and destroy arm (c) sessions.

Two modes, because the API reference does not list `custom` among the agent
kinds CreateSession accepts (it may be gated per team):

  host    (default) Boot a standard adapter's sandbox and run our daemon
          inside it next to the adapter's own (idle) agent process.
  custom  Boot our own container image (Dockerfile here), daemon = entrypoint.

Usage (run from repo root; needs DIGITALOCEAN_TOKEN, MODEL_* env):
  python3 arms/managed_agents/bootstrap.py create  --name dc-c-2pct [--mode host|custom]
  python3 arms/managed_agents/bootstrap.py install --name dc-c-2pct      # host mode
  python3 arms/managed_agents/bootstrap.py smoke   --name dc-c-2pct
  python3 arms/managed_agents/bootstrap.py pause   --name dc-c-2pct
  python3 arms/managed_agents/bootstrap.py destroy --name dc-c-2pct
  python3 arms/managed_agents/bootstrap.py reset   --name dc-c-2pct      # fresh daemon
  python3 arms/managed_agents/bootstrap.py up --names a,b,c   # create+install+smoke+reset+pause
"""

import argparse
import io
import json
import os
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from driver.hr_api import HarnessRuntime  # noqa: E402

PORT = "8080"
# Resolved inside the sandbox by `install`; custom images use the fixed path.
CUSTOM_DIR = "/opt/dcagent"


def q(v):
    return json.dumps(str(v))  # a JSON string is a valid YAML scalar


def spec_yaml(a):
    env = {"MODEL_BASE_URL": os.environ.get("MODEL_BASE_URL",
                                            "https://inference.do-ai.run/v1"),
           "MODEL_ID": os.environ.get("MODEL_ID", ""), "PORT": PORT,
           "AGENT_ID": a.name}
    lines = [f"name: {q(a.name)}",
             f"description: {q('duty-cycle experiment arm (c)')}",
             f"size: {q(a.size)}",
             # keep_warm and idle_timeout are mutually exclusive in the spec
             "keep_warm: true" if getattr(a, "keep_warm", False)
             else f"idle_timeout: {q(a.idle_timeout)}",
             "labels:", f"  experiment: {q('agent-duty-cycle')}",
             "env:"] + [f"  {k}: {q(v)}" for k, v in env.items()]
    # Default: NO secrets in the spec. Each spec secret is written to the
    # team's credential storage on every create, and that storage started
    # failing in Team B (P-002). The model key is instead copied into the
    # sandbox at install time (see install_key). --secret-mode spec restores
    # the documented way.
    if getattr(a, "secret_mode", "file") == "spec":
        lines += ["secrets:",
                  f"  MODEL_ACCESS_KEY: {q(os.environ.get('MODEL_ACCESS_KEY', ''))}"]
    lines += ["permissions:", "  default: allow"]
    if a.mode == "custom":
        if not a.image or "@sha256:" not in a.image:
            sys.exit("--image must be digest-pinned (registry/repo@sha256:...)")
        lines[1:1] = ["agent: custom", f"image: {q(a.image)}",
                      'entrypoint: ["python3", "-m", "core.server"]']
    else:
        lines[1:1] = [f"agent: {q(a.adapter)}"]
    return "\n".join(lines) + "\n"


def session_id(hr, name):
    s = hr.find_by_name(name)
    if not s:
        sys.exit(f"no session named {name}")
    return s["session_id"]


def ensure_ready(hr, sid):
    st = hr.status(sid)
    if st == "SESSION_STATUS_PAUSED":
        hr.resume(sid)
    return hr.wait_status(sid)


def code_tarball():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for f in ("__init__.py", "agent.py", "model.py", "state.py", "timing.py",
                  "server.py", "deliver.py"):
            t.add(ROOT / "core" / f, arcname=f"core/{f}")
        t.add(ROOT / "arms/managed_agents/start_daemon.py", arcname="start_daemon.py")
    return buf.getvalue()


RESOLVE = ("import os,json;h=os.path.expanduser('~');"
           "w='/workspace' if os.access('/workspace',os.W_OK) else h;"
           "print(json.dumps({'dir':h+'/dcagent','ws':w+'/agent-ws',"
           "'python':__import__('sys').version.split()[0]}))")


def paths(hr, sid, mode):
    if mode == "custom":
        return {"dir": CUSTOM_DIR, "ws": "/workspace/agent-ws"}
    r = hr.exec(sid, ["python3", "-c", RESOLVE], timeout_s=30)
    if r.get("exit_code") != 0:
        sys.exit(f"python3 not usable in sandbox: {r}")
    return json.loads(r["stdout"])


def cmd_create(hr, a):
    if hr.find_by_name(a.name):
        print(f"{a.name}: exists")
        return
    s = hr.create_from_yaml(spec_yaml(a))
    print(json.dumps({"created": a.name, "session_id": s.get("session_id"),
                      "warnings": s.get("warnings")}))
    t = time.time()
    hr.wait_status(s["session_id"])
    print(f"{a.name}: READY after {time.time() - t:.1f}s")


def cmd_install(hr, a):
    sid = session_id(hr, a.name)
    ensure_ready(hr, sid)
    p = paths(hr, sid, a.mode)
    if a.mode == "host":
        r = hr.exec(sid, ["sh", "-c", f'mkdir -p "{p["dir"]}" && tar -xzf - -C "{p["dir"]}"'],
                    stdin=code_tarball(), timeout_s=60)
        if r.get("exit_code") != 0:
            sys.exit(f"upload failed: {r}")
        if getattr(a, "secret_mode", "file") == "file":
            install_key(hr, sid, p["dir"])
        r = hr.exec(sid, ["python3", f'{p["dir"]}/start_daemon.py', p["dir"], p["ws"]],
                    timeout_s=60)
        print(r.get("stdout") or r)
        if r.get("exit_code") != 0:
            sys.exit("daemon failed to start")
    print(json.dumps({"installed": a.name, **p}))


def install_key(hr, sid, d):
    """Write the model key to <dir>/.model_key (mode 600) inside the sandbox.
    Sent over the authenticated exec API, never in argv or the spec."""
    key = os.environ.get("MODEL_ACCESS_KEY", "")
    if not key:
        sys.exit("MODEL_ACCESS_KEY is not set on the driver")
    r = hr.exec(sid, ["sh", "-c", f'umask 077 && cat > "{d}/.model_key"'],
                stdin=key.encode(), timeout_s=30)
    if r.get("exit_code") != 0:
        sys.exit(f"writing model key failed: {r}")


def deliver(hr, sid, d, ev, wait=200):
    r = hr.exec(sid, ["python3", f"{d}/core/deliver.py", "submit", str(wait)],
                stdin=json.dumps(ev).encode(), timeout_s=int(wait) + 30)
    return json.loads(r.get("stdout") or "{}")


def cmd_smoke(hr, a):
    """Checks everything the week-long run depends on. Prints one JSON report."""
    sid = session_id(hr, a.name)
    rep = {"name": a.name, "session_id": sid}
    ensure_ready(hr, sid)
    p = paths(hr, sid, a.mode)
    rep["paths"] = p
    h = hr.exec(sid, ["python3", "-c",
                      "import os,urllib.request as u;print(u.urlopen('http://127.0.0.1:'"
                      f"+os.environ.get('PORT','{PORT}')+'/health').read().decode())"],
                timeout_s=30)
    rep["health_before"] = json.loads(h.get("stdout") or "{}")
    out = deliver(hr, sid, p["dir"], {"id": f"smoke-{time.time_ns()}", "type": "probe"})
    rep["probe_ok"], rep["probe_error"] = out.get("ok"), out.get("detail")
    rep["probe_tokens"] = out.get("tokens")
    pid_before = (out.get("process") or {}).get("pid")
    helper_before = (out.get("liveness") or {}).get("helper_pid")
    t = time.perf_counter()
    hr.pause(sid)
    rep["pause_call_s"] = round(time.perf_counter() - t, 3)
    hr.wait_status(sid, ("SESSION_STATUS_PAUSED",), timeout=120, every=1)
    rep["paused_after_s"] = round(time.perf_counter() - t, 3)
    time.sleep(a.pause_seconds)
    t = time.perf_counter()
    hr.resume(sid)
    rep["resume_call_s"] = round(time.perf_counter() - t, 3)
    out = deliver(hr, sid, p["dir"], {"id": f"smoke-{time.time_ns()}", "type": "probe"})
    rep["resume_to_first_result_s"] = round(time.perf_counter() - t, 3)
    rep["process_survived_pause"] = (out.get("process") or {}).get("pid") == pid_before
    rep["helper_survived_pause"] = (out.get("liveness") or {}).get("helper_pid") == helper_before \
        and not (out.get("liveness") or {}).get("helper_restarted")
    rep["model_conn_reused_after_pause"] = (out.get("model_calls") or [{}])[0].get("conn_reused")
    rep["events_processed"] = out.get("events_processed")
    print(json.dumps(rep, indent=2))


RESET = ("import json,os,signal,time,urllib.request as u;"
         "p=os.environ.get('PORT','8080');"
         "h=json.loads(u.urlopen('http://127.0.0.1:'+p+'/health').read());"
         "os.killpg(h['pid'],signal.SIGTERM);time.sleep(1);print(json.dumps({'killed':h['pid']}))")


def cmd_reset(hr, a):
    """Restart the daemon with empty state, so smoke-test events (history,
    counters) never leak into the measured run. Host mode only."""
    sid = session_id(hr, a.name)
    ensure_ready(hr, sid)
    p = paths(hr, sid, a.mode)
    r = hr.exec(sid, ["python3", "-c", RESET], timeout_s=30)
    print(r.get("stdout") or r.get("stderr"))
    r = hr.exec(sid, ["sh", "-c", f'rm -rf "{p["ws"]}"'], timeout_s=30)
    r = hr.exec(sid, ["python3", f'{p["dir"]}/start_daemon.py', p["dir"], p["ws"]],
                timeout_s=60)
    out = json.loads(r.get("stdout") or "{}")
    if not out.get("started") or out.get("events_processed") != 0:
        sys.exit(f"reset failed: {out}")
    print(json.dumps({"reset": a.name, "pid": out.get("pid")}))


def cmd_redeploy(hr, a):
    """Push new core code into an existing host-mode session and restart its
    daemon with EMPTY state, then pause it. Used after a core fix mid-study."""
    cmd_install(hr, a)      # uploads code + key; old daemon keeps running
    cmd_reset(hr, a)        # kills it, wipes the workspace, starts the new code
    hr.pause(session_id(hr, a.name))
    print(json.dumps({"redeployed": a.name}))


def cmd_cleanup_configs(hr, a):
    """Delete saved Environment Configs whose name starts with --prefix
    (default dc-). Never touches anyone else's configs."""
    pre = a.prefix or "dc-"
    if not pre.startswith("dc-"):
        sys.exit("refusing: prefix must start with dc-")
    mine = [c for c in hr.list_configs() if str(c.get("name", "")).startswith(pre)]
    for c in mine:
        cid = c.get("id") or c.get("config_id")
        hr.delete_config(cid)
        print(f"deleted config {c.get('name')} ({cid})")
    print(json.dumps({"deleted": len(mine), "prefix": pre}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["create", "install", "smoke", "reset", "pause",
                                    "destroy", "up", "cleanup-configs", "redeploy"])
    ap.add_argument("--prefix", help="cleanup-configs: name prefix (must start dc-)")
    ap.add_argument("--name")
    ap.add_argument("--names", help="comma-separated, for `up`")
    ap.add_argument("--mode", choices=["host", "custom"], default="host")
    ap.add_argument("--adapter", default="opencode", help="host mode adapter")
    ap.add_argument("--image", help="custom mode: registry/repo@sha256:...")
    ap.add_argument("--size", default="mars-2vcpu-4gb")
    ap.add_argument("--idle-timeout", default="15m")
    ap.add_argument("--pause-seconds", type=float, default=10)
    ap.add_argument("--secret-mode", choices=["file", "spec"], default="file",
                    help="file: copy model key into the sandbox (default); spec: spec secrets")
    a = ap.parse_args()
    hr = HarnessRuntime()
    if a.cmd == "redeploy":
        for n in (a.names or a.name).split(","):
            a.name = n.strip()
            cmd_redeploy(hr, a)
        return
    if a.cmd == "up":
        for n in a.names.split(","):
            a.name = n.strip()
            cmd_create(hr, a)
            cmd_install(hr, a)
            cmd_smoke(hr, a)
            if a.mode == "host":
                cmd_reset(hr, a)
            hr.pause(session_id(hr, a.name))
        return
    if a.cmd == "pause":
        return hr.pause(session_id(hr, a.name))
    if a.cmd == "destroy":
        return hr.delete(session_id(hr, a.name))
    {"create": cmd_create, "install": cmd_install, "smoke": cmd_smoke,
     "reset": cmd_reset, "cleanup-configs": cmd_cleanup_configs}[a.cmd](hr, a)


if __name__ == "__main__":
    main()
