"""Unit tests (no network, no servers).   python3 -m unittest discover -s tests"""

import datetime
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FN = ROOT / "arms/functions/packages/agent/handle"
sys.path[:0] = [str(ROOT), str(FN)]

import s3mini  # noqa: E402
import state_store as ss  # noqa: E402
from analysis.analyze import union_len, pct  # noqa: E402
from core.state import AgentState  # noqa: E402
from db import Db  # noqa: E402
from driver import schedule  # noqa: E402
from local_blobs import LocalBlobs  # noqa: E402


class SigV4(unittest.TestCase):
    def test_aws_get_vanilla_vector(self):
        # AWS SigV4 test suite "get-vanilla"
        h = s3mini.sigv4_headers(
            "GET", "example.amazonaws.com", "/", "us-east-1", "AKIDEXAMPLE",
            "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            payload_hash=hashlib.sha256(b"").hexdigest(), service="service",
            now=datetime.datetime(2015, 8, 30, 12, 36, 0, tzinfo=datetime.timezone.utc))
        self.assertIn("Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
                      h["Authorization"])
        self.assertIn("SignedHeaders=host;x-amz-date,", h["Authorization"])


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.db = Db(f"sqlite:///{self.d}/a.db")
        self.db.ensure_schema()
        self.blobs = LocalBlobs(f"{self.d}/blobs")
        self.store = ss.StateStore(self.db, self.blobs, f"{self.d}/tmp")

    def roundtrip(self, mutate):
        ph = {}
        st, ws, meta = self.store.load("x", ph)
        mutate(st, ws)
        self.store.save(st, ws, meta, ph)
        return ph

    def test_roundtrip_state_and_files(self):
        def m1(st, ws):
            st.incidents["i1"] = {"status": "awaiting_approval"}
            st.history.append({"role": "user", "content": "hi"})
            (ws / "reports").mkdir()
            (ws / "reports/i1.md").write_text("plan")
        self.roundtrip(m1)
        st, ws, meta = self.store.load("x", {})
        self.assertEqual(st.incidents["i1"]["status"], "awaiting_approval")
        self.assertEqual((ws / "reports/i1.md").read_text(), "plan")
        self.assertEqual(meta["version"], 1)

    def test_unchanged_workspace_not_reuploaded(self):
        self.roundtrip(lambda st, ws: (ws / "f").write_text("a"))
        ph = self.roundtrip(lambda st, ws: setattr(st, "feed_cursor", 5))
        self.assertEqual(ph["bytes_up"], 0)

    def test_old_snapshot_garbage_collected(self):
        self.roundtrip(lambda st, ws: (ws / "f").write_text("a"))
        self.roundtrip(lambda st, ws: (ws / "f").write_text("bb"))
        self.assertEqual(self.blobs.keys(), ["agents/x/ws-2.tar"])

    def test_stale_version_conflicts(self):
        self.roundtrip(lambda st, ws: None)
        st1, ws1, m1 = self.store.load("x", {})
        st2, ws2, m2 = self.store.load("x", {})
        self.store.save(st1, ws1, m1, {})
        with self.assertRaises(ss.Conflict):
            self.store.save(st2, ws2, m2, {})

    def test_unknown_schema_rejected(self):
        with self.assertRaises(ValueError):
            ss.state_from_json(ss.state_to_json(AgentState()), 99)

    def test_signature_stable_across_processes(self):
        # Regression for B-001: hash() is salted per process.
        ws = Path(tempfile.mkdtemp())
        (ws / "f").write_text("a")
        sig = ss.workspace_signature(ws)
        code = (f"import sys;sys.path.insert(0,{str(FN)!r});sys.path.insert(0,{str(ROOT)!r});"
                f"import state_store as s;print(s.workspace_signature({str(ws)!r}))")
        out = os.popen(f"{sys.executable} -c \"{code}\"").read().strip()
        self.assertEqual(sig, out)


class ScheduleTests(unittest.TestCase):
    def gen(self, target, profile="2pct"):
        p = tempfile.mktemp()
        schedule.main(["main", "--profile", profile, "--days", "1", "--target", target,
                       "--out", p])
        return [json.loads(x) for x in open(p)]

    def test_identical_content_across_targets(self):
        a, b = self.gen("a-2pct"), self.gen("b-2pct")
        strip = lambda e: {k: v for k, v in e.items() if k not in ("id", "target")}
        self.assertEqual([strip(e) for e in a], [strip(e) for e in b])

    def test_approvals_follow_incidents(self):
        ev = self.gen("a", "30pct")
        seen = {}
        for e in ev:
            if e["type"] == "incident":
                seen[e["incident"]["id"]] = e["offset_s"]
            if e["type"] == "approval":
                self.assertIn(e["incident_id"], seen)
                self.assertGreater(e["offset_s"], seen[e["incident_id"]])

    def test_event_length_cap(self):
        for e in self.gen("a", "30pct"):
            w = e["work"]
            self.assertLessEqual(schedule.est(w, 2.5), schedule.MAX_EVENT_S + 1e-6)


class AnalysisTests(unittest.TestCase):
    def test_union(self):
        self.assertEqual(union_len([(0, 2), (1, 3), (5, 6)]), 4)

    def test_target_with_an_error_row(self):
        # Regression for B-009: an error row overwrote the arm-kind variable.
        from types import SimpleNamespace
        from analysis.analyze import analyze_target
        row = lambda i, ok: {"event_id": f"e{i}", "type": "probe", "kind": "managed_agents",
                             "sent_wall": 100.0 + i, "start_wall": 100.0 + i,
                             "finish_wall": 101.0 + i, "driver_total_s": 1.0,
                             "awake_end_wall": 101.0 + i, "paused": True,
                             "result": {"ok": ok, "error": None if ok else "ModelError",
                                        "timing": {"total_s": 0.5, "cpu_s": 0.1}}}
        out = analyze_target("c-x", [row(0, True), row(1, False)],
                             {"start_wall": 100.0, "end_wall": 110.0, "speed": 1.0},
                             SimpleNamespace(fn_mem_gib=0.5, idle_timeout=900, ma_vcpu=2, ma_mem_gb=4))
        self.assertEqual(out["correctness"]["errors"], {"ModelError": 1})
        self.assertIn("managed_agents", out)

    def test_pct(self):
        self.assertEqual(pct([1, 2, 3, 4], 50), 2.5)


class CostModelTests(unittest.TestCase):
    def test_state_backends_include_pg_storage(self):
        # The 27 Oct-8 Nov bill showed Postgres storage as its own line.
        from analysis import cost_model as cm
        self.assertAlmostEqual(cm.state_backends_month(), 22.15, places=2)

    def test_week2_stateless_estimates(self):
        # Billed-awake 1.96% and 33.04% of 730 h at 512 MB, as published.
        from analysis import cost_model as cm
        self.assertAlmostEqual(cm.functions_month(0.0196 * 730 * 3600), 22.63, places=2)
        self.assertAlmostEqual(cm.functions_month(0.3304 * 730 * 3600), 30.18, places=2)

    def test_article_model_matches_repo_model(self):
        sys.path.insert(0, str(ROOT / "article"))
        import duty_cost
        from analysis import cost_model as cm
        self.assertAlmostEqual(duty_cost.BACKENDS, cm.state_backends_month(), places=6)
        self.assertAlmostEqual(duty_cost.always_on(0.5), cm.droplet_month(), places=6)
        # Published table points at 2% / 30% with 11% awake CPU (includes SESSION_STORAGE).
        self.assertAlmostEqual(duty_cost.stateless(0.02), 22.64, places=2)
        self.assertAlmostEqual(duty_cost.pause_resume(0.02), 0.75, places=2)


class ArticleDutyCycleTests(unittest.TestCase):
    def test_sample_events_match_article_example(self):
        # article.md: python3 duty_cycle.py events.jsonl 1730000000 1730007200 → 2.03%
        import subprocess
        out = subprocess.check_output(
            [sys.executable, str(ROOT / "article/duty_cycle.py"),
             str(ROOT / "article/sample_events.jsonl"),
             "1730000000", "1730007200"],
            text=True,
        ).strip()
        self.assertEqual(out, "events: 4  duty cycle: 2.03%")

    def test_duty_cycle_merges_overlaps(self):
        sys.path.insert(0, str(ROOT / "article"))
        import duty_cycle as dc
        # [0,50] ∪ [20,50] ∪ [3000,3036] ∪ [7140,7200] over 7200 s → 2.03%
        intervals = [(0.0, 30.0), (20.0, 50.0), (3000.0, 3036.0), (7140.0, 7200.0)]
        self.assertAlmostEqual(dc.duty_cycle(intervals, 7200.0), 0.0202777, places=5)


if __name__ == "__main__":
    unittest.main()
