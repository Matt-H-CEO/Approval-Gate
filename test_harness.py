import json
import unittest

from harness import judging, seal
from harness.runner import Stack


def item(n, label, expected, text="Hello."):
    return {"id": f"t{n}", "text": text, "context": {}, "label": label, "expected_decision": expected, "tags": []}


class SealTests(unittest.TestCase):
    def test_round_trip_wrong_key_and_tamper(self):
        envelope = seal.seal_json({"a": [1, 2, 3]}, "correct horse", iterations=1000)
        self.assertEqual(seal.unseal_json(envelope, "correct horse"), {"a": [1, 2, 3]})
        with self.assertRaises(seal.SealError):
            seal.unseal_json(envelope, "wrong")
        envelope["ciphertext"] = envelope["ciphertext"][:-4] + "AAAA"
        with self.assertRaises(seal.SealError):
            seal.unseal_json(envelope, "correct horse")

    def test_ciphertext_hides_plaintext(self):
        envelope = seal.seal(b"held-out secret text", "k", iterations=1000)
        self.assertNotIn("held-out", envelope["ciphertext"])


class ReportTests(unittest.TestCase):
    def test_sign_verify_detects_edits(self):
        result = {"team": "T", "resilience": {"points": 7.5}, "meta": {}}
        judging.sign(result, "key")
        self.assertTrue(judging.verify(result, "key")[0])
        self.assertFalse(judging.verify(result, "other")[0])
        result["resilience"]["points"] = 15
        self.assertFalse(judging.verify(result, "key")[0])

    def test_team_names_are_path_safe(self):
        for bad in ("", " x", "a/b", "..", "a|b", "x" * 101):
            with self.assertRaises(ValueError):
                judging.safe_name(bad)
        self.assertEqual(judging.safe_name("Team 7"), "Team 7")

    def test_surprises_rotate_by_team_and_cover_both_labels(self):
        pool = [item(i, "good" if i % 2 else "bad", "APPROVE" if i % 2 else "BLOCK") for i in range(4)]
        picks = judging.pick_surprises(pool, "Alpha")
        self.assertEqual(sorted(p["label"] for p in picks), ["bad", "good"])
        self.assertEqual(picks, judging.pick_surprises(pool, "Alpha"))

    def test_metrics_follow_the_judging_guide(self):
        rows = [(item(1, "bad", "BLOCK"), {"ok": True, "decision": "HOLD_FOR_REVIEW", "seconds": 1}),
                (item(2, "good", "APPROVE"), {"ok": False, "error": "x", "seconds": 1}),
                (item(3, "good", "APPROVE"), {"ok": True, "decision": "APPROVE", "seconds": 1})]
        m = judging.metrics(rows)
        self.assertEqual((m["caught"], m["false_blocks"], m["exact"], m["errors"]), (1, 1, 1, 1))


class ResultsViewerTests(unittest.TestCase):
    def test_viewer_serves_newest_result_and_rejects_unknown_files(self):
        import os, tempfile, threading, urllib.request
        from pathlib import Path
        from unittest.mock import patch
        from http.server import ThreadingHTTPServer
        from harness import results_server
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory, "old.json"), Path(directory, "new.json")
            old.write_text(json.dumps({"team": "a", "mode": "practice", "meta": {"finished": "2026-01-01T00:00:00"}}))
            new.write_text(json.dumps({"team": "b", "mode": "final", "meta": {"finished": "2026-01-02T00:00:00"}}))
            os.utime(old, (1, 1))
            with patch.object(results_server, "RESULTS", Path(directory)):
                server = ThreadingHTTPServer(("127.0.0.1", 0), results_server.Handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.addCleanup(server.server_close)
                self.addCleanup(server.shutdown)
                base = f"http://127.0.0.1:{server.server_port}"
                get = lambda path: json.load(urllib.request.urlopen(base + path, timeout=3))
                self.assertEqual(get("/api/results")["selected"], "new.json")
                self.assertEqual(get("/api/results?file=old.json")["result"]["team"], "a")
                self.assertEqual(get("/api/results?file=..%2Fsecret")["selected"], "new.json")
                self.assertIn(b"Approval Gate results", urllib.request.urlopen(base + "/", timeout=3).read())


class StackTests(unittest.TestCase):
    def test_skeleton_runs_end_to_end_on_loopback(self):
        practice = [item(1, "good", "APPROVE", "Thanks for your patience."),
                    item(2, "bad", "BLOCK", "Revenue grew 22% year over year.")]
        result = judging.evaluate("customer_support", "unit-test", practice, latency="0", modes=["unavailable", "malformed"],
                                  say=lambda *_: None)
        self.assertEqual(result["measured"]["practice"]["errors"], 0)
        self.assertEqual(result["resilience"]["points"], 5.0)
        self.assertNotIn("gate_admin", json.dumps(result))

    def test_wrapper_never_sees_the_admin_key(self):
        import urllib.request
        with Stack(latency="0") as stack:
            self.assertEqual(stack.wrapper_url.split(":")[1], "//127.0.0.1")
            self.assertNotIn(stack.admin_key, stack.wrapper_stderr_tail())
            with urllib.request.urlopen(stack.wrapper_url + "/health", timeout=5) as response:
                self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
