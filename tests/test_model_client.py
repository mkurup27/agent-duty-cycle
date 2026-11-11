"""ModelClient: retries transient 529s and evicts idle connections."""
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.model import ModelClient  # noqa: E402

PORT = 9560


class ModelClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = subprocess.Popen([sys.executable, "local/mock_inference.py", "--port",
                                  str(PORT), "--latency", "0.01", "--fail-first", "2"],
                                 cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.8)

    @classmethod
    def tearDownClass(cls):
        cls.p.kill()

    def test_retry_then_evict(self):
        m = ModelClient(base_url=f"http://127.0.0.1:{PORT}/v1", model_id="m", api_key="k")
        m.BACKOFF_S = (0.05, 0.05, 0.05)
        _, rec = m.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(rec["retries"], 2)           # two 529s, then success
        _, rec = m.chat([{"role": "user", "content": "again"}])
        self.assertTrue(rec["conn_reused"])
        self.assertFalse(rec["conn_evicted_idle"])
        m.IDLE_EVICT_S = 0.1
        time.sleep(0.3)
        _, rec = m.chat([{"role": "user", "content": "after idle"}])
        self.assertTrue(rec["conn_evicted_idle"])
        self.assertFalse(rec["conn_reused"])


if __name__ == "__main__":
    unittest.main()
