"""Integration tests against the local stand-ins (spawns servers on 95xx ports).

  python3 -m unittest tests.test_integration -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "arms/managed_agents")]

import bootstrap  # noqa: E402
from driver.hr_api import HarnessRuntime, HRError  # noqa: E402
from driver.targets import FunctionsTarget, ManagedAgentsTarget  # noqa: E402

MI, FNP, HRP = 9510, 9520, 9530


def spawn(args):
    return subprocess.Popen([sys.executable] + args, cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Stack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sd = tempfile.mkdtemp()
        cls.procs = [
            spawn(["local/mock_inference.py", "--port", str(MI), "--latency", "0.01"]),
            # keepalive ~0: every invocation lands in a fresh (cold) container
            spawn(["local/fake_functions.py", "--port", str(FNP), "--state-dir", cls.sd,
                   "--keepalive", "0.05", "--blocking-window", "0.3",
                   "--model-url", f"http://127.0.0.1:{MI}/v1"]),
            spawn(["local/fake_harness.py", "--port", str(HRP), "--resume-delay", "0.05"]),
        ]
        os.environ.update(DO_API_BASE=f"http://127.0.0.1:{HRP}", DIGITALOCEAN_TOKEN="t",
                          MODEL_BASE_URL=f"http://127.0.0.1:{MI}/v1", MODEL_ID="mock",
                          MODEL_ACCESS_KEY="k", FN_KEY_ID="a", FN_KEY_SECRET="b")
        time.sleep(1.0)

    @classmethod
    def tearDownClass(cls):
        for p in cls.procs:
            p.kill()
        # Stop sandbox daemons the fake harness left behind. Prefer .fake_pids
        # (portable); /proc environ works on Linux. macOS `ps eww` does not
        # show FAKE_SANDBOX=, so do not rely on it.
        for pid_file in Path(tempfile.gettempdir()).glob("fake-sbx-*/.fake_pids"):
            try:
                for line in pid_file.read_text().splitlines():
                    if not line.strip().isdigit():
                        continue
                    pid = int(line.strip())
                    try:
                        os.kill(pid, 18)  # SIGCONT (may be paused)
                        os.kill(pid, 9)
                    except OSError:
                        pass
            except OSError:
                pass
            shutil.rmtree(pid_file.parent, ignore_errors=True)
        if Path("/proc").is_dir():
            for d in Path("/proc").glob("[0-9]*"):
                try:
                    if b"FAKE_SANDBOX=" in (d / "environ").read_bytes():
                        os.kill(int(d.name), 18)
                        os.kill(int(d.name), 9)
                except OSError:
                    pass
        shutil.rmtree(cls.sd, ignore_errors=True)

    # ---------------------------------------------------------- Functions
    def fn(self, action="agent/t1"):
        return FunctionsTarget("t", {"apihost": f"http://127.0.0.1:{FNP}",
                                     "namespace": "ns", "action": action})

    def test_stateless_state_survives_cold_containers(self):
        t = self.fn()
        seq = [{"id": "e1", "type": "incident", "work": {"model_calls": 1},
                "incident": {"id": "i1", "service": "db", "severity": "high",
                             "summary": "slow"}},
               {"id": "e2", "type": "tick", "feed": [], "work": {"model_calls": 1}},
               {"id": "e3", "type": "approval", "incident_id": "i1",
                "decision": "approve", "work": {"model_calls": 1}}]
        pids = set()
        for ev in seq:
            r = t.deliver(ev, 999)
            time.sleep(0.8)  # let the container die
            self.assertIsNone(r["error"], r)
            self.assertTrue(r["result"]["ok"], r["result"])
            self.assertTrue(r["result"]["process"]["cold_process"])
            self.assertTrue(r["result"]["liveness"]["helper_restarted"])
            self.assertIsNotNone(r["fn_init_ms"])
            pids.add(r["result"]["process"]["pid"])
        self.assertEqual(len(pids), 3)
        self.assertEqual(r["result"]["events_processed"], 3)
        self.assertEqual(r["result"]["result"]["status"], "resolved")

    def test_long_invocation_converts_to_async(self):
        t = self.fn("agent/t2")
        r = t.deliver({"id": "x1", "type": "probe", "work": {"model_calls": 0,
                                                            "io_wait_s": 0.8}}, 999)
        self.assertEqual(r.get("invoke"), "async")
        self.assertGreaterEqual(r["polls"], 1)
        self.assertTrue(r["result"]["ok"])
        self.assertGreaterEqual(r["fn_duration_ms"], 800)

    # ---------------------------------------------------------- Harness Runtime
    def ns(self, name, keep_warm=False, idle="15m"):
        return SimpleNamespace(name=name, mode="host", adapter="opencode", image=None,
                               size="mars-2vcpu-4gb", idle_timeout=idle,
                               keep_warm=keep_warm, pause_seconds=0.2)

    def test_pause_resume_keeps_process_and_state(self):
        hr = HarnessRuntime()
        a = self.ns("it-c1")
        bootstrap.cmd_create(hr, a)
        bootstrap.cmd_install(hr, a)
        t = ManagedAgentsTarget("c", {"session": "it-c1", "wait_s": 1,
                                      "min_gap_to_pause_s": 0})
        r1 = t.deliver({"id": "c1", "type": "incident", "work": {"model_calls": 1},
                        "incident": {"id": "i9", "service": "x", "severity": "high",
                                     "summary": "s"}}, 999)
        self.assertTrue(r1["paused"])
        r2 = t.deliver({"id": "c2", "type": "approval", "incident_id": "i9",
                        "work": {"model_calls": 1, "io_wait_s": 1.5}}, 999)
        self.assertEqual(r2["status_before"], "SESSION_STATUS_PAUSED")
        self.assertIsNotNone(r2["resume_s"])
        self.assertGreaterEqual(r2["polls"], 1)        # ran past wait_s
        self.assertTrue(r2["result"]["ok"], r2["result"])
        self.assertEqual(r1["result"]["process"]["pid"], r2["result"]["process"]["pid"])
        self.assertFalse(r2["result"]["liveness"]["helper_restarted"])
        self.assertTrue(r2["result"]["model_calls"][0]["conn_reused"])
        hr.delete(bootstrap.session_id(hr, a.name))

    def test_reset_clears_smoke_state(self):
        hr = HarnessRuntime()
        a = self.ns("it-c2")
        bootstrap.cmd_create(hr, a)
        bootstrap.cmd_install(hr, a)
        bootstrap.cmd_smoke(hr, a)
        bootstrap.cmd_reset(hr, a)
        t = ManagedAgentsTarget("c", {"session": "it-c2", "wait_s": 1})
        r = t.deliver({"id": "z", "type": "probe", "work": {"model_calls": 0}}, 0)
        self.assertEqual(r["result"]["events_processed"], 1)
        hr.delete(bootstrap.session_id(hr, a.name))

    def test_keep_warm_rejects_pause_and_idle_autopause(self):
        hr = HarnessRuntime()
        sw = hr.create_from_yaml(bootstrap.spec_yaml(self.ns("it-warm", keep_warm=True)))
        hr.wait_status(sw["session_id"], every=0.2)
        with self.assertRaises(HRError) as cm:
            hr.pause(sw["session_id"])
        self.assertEqual(cm.exception.status, 409)
        si = hr.create_from_yaml(bootstrap.spec_yaml(self.ns("it-idle", idle="1s")))
        hr.wait_status(si["session_id"], ("SESSION_STATUS_PAUSED",), timeout=10, every=0.2)
        self.assertEqual(hr.get(si["session_id"])["pause_reason"], "idle")
        hr.delete(sw["session_id"])
        hr.delete(si["session_id"])


if __name__ == "__main__":
    unittest.main()
