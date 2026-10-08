import importlib
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import mock_ezio

sys.path.insert(0, str(Path(__file__).resolve().parent / "wrapper"))


class HTTPTests(unittest.TestCase):
    def start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def post(self, base, path, body, headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=raw,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_judge_controls_are_private_and_override_headers(self):
        with patch.multiple(mock_ezio, ADMIN=True, ADMIN_KEY="test-judge-only", LAT_MIN=0, LAT_MAX=0):
            base = self.start_server(mock_ezio.Handler)
            participant = {"Authorization": "Bearer mock-team-key"}
            judge = {"Authorization": "Bearer test-judge-only"}
            self.assertEqual(self.post(base, "/admin/mode", {"mode": "down"}, participant)[0], 401)
            self.assertEqual(self.post(base, "/admin/mode", {"mode": "bogus"}, judge)[0], 400)
            self.assertEqual(self.post(base, "/admin/mode", {"mode": "down"}, judge)[0], 200)
            code, _ = self.post(base, "/v1/analyze", {"text": "Hello"},
                                {**participant, "X-Mock-Scenario": "unavailable"})
            self.assertEqual(code, 503)
            for _ in range(2):
                self.assertEqual(self.post(base, "/admin/mode", {"mode": "rate_limited"}, judge)[0], 200)
                self.assertEqual(self.post(base, "/v1/analyze", {"text": "Hello"}, participant)[0], 429)
                self.assertEqual(self.post(base, "/v1/analyze", {"text": "Hello"}, participant)[0], 200)
            self.assertEqual(self.post(base, "/admin/mode", {"mode": "none"}, judge)[0], 200)
            code, body = self.post(base, "/v1/analyze", {"text": "Hello"},
                                   {**participant, "X-Mock-Scenario": "down"})
            self.assertEqual(code, 200)
            self.assertIn("dimensions", body)

    def test_persona_page_samples_and_static_files(self):
        serve = importlib.import_module("serve")
        base = self.start_server(serve.Handler)
        with urllib.request.urlopen(base + "/", timeout=3) as response:
            page = response.read().decode()
            self.assertIn("text/html", response.headers["Content-Type"])
        self.assertIn("/decide", page)
        self.assertNotIn("/v1/analyze", page)
        with urllib.request.urlopen(base + "/api/samples?persona=paralegal", timeout=3) as response:
            samples = json.load(response)["samples"]
        self.assertTrue(samples and set(samples[0]) == {"id", "text", "context"})
        for path in ("/api/samples?persona=nobody", "/../wrapper.py", "/%2e%2e/wrapper.py", "/missing.js"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + path, timeout=3)
            self.assertIn(caught.exception.code, (400, 404))

    def test_admin_disabled_by_default(self):
        base = self.start_server(mock_ezio.Handler)
        self.assertEqual(self.post(base, "/admin/mode", {"mode": "down"})[0], 404)

    def test_wrapper_validates_requests_and_gates_actions(self):
        serve = importlib.import_module("serve")
        wrapper = importlib.import_module("wrapper")
        base = self.start_server(serve.Handler)
        with patch.object(serve, "decide", return_value=wrapper.Decision("APPROVE", "checked", ["evidence"], {})) as decide, \
                patch.object(serve.mock_actions, "run_for_persona") as action:
            payload = {"text": "Hello", "persona": "customer_support", "context": {}}
            code, body = self.post(base, "/decide", payload)
            self.assertEqual((code, body["decision"]), (200, "APPROVE"))
            self.assertTrue(body["action_executed"])
            action.assert_called_once_with("customer_support", "Hello")
            decide.return_value = wrapper.Decision("BLOCK", "wrong", ["evidence"], {})
            self.assertFalse(self.post(base, "/decide", payload)[1]["action_executed"])
            action.assert_called_once()
            for bad in ([], {}, {**payload, "persona": "../invalid"}, {**payload, "context": []},
                        {**payload, "text": " "}, b"{broken"):
                self.assertEqual(self.post(base, "/decide", bad)[0], 400)
            self.assertEqual(self.post(base, "/other", payload)[0], 404)
            self.assertEqual(self.post(base, "/decide", {**payload, "text": "x" * 20001})[0], 400)

    def test_log_dir_redirects_audit_and_actions(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"GATE_LOG_DIR": directory}):
            audit = importlib.reload(importlib.import_module("audit"))
            actions = importlib.reload(importlib.import_module("mock_actions"))
            self.assertEqual(audit.LOG.parent, Path(directory))
            self.assertEqual(actions.LOG.parent, Path(directory))
        importlib.reload(importlib.import_module("audit"))
        importlib.reload(importlib.import_module("mock_actions"))

    def test_malformed_scores_fail_closed(self):
        wrapper = importlib.import_module("wrapper")
        body, _ = mock_ezio.analyze("Hello", "")
        for mutation in (lambda b: b["verdict"].update(tier="PURPLE"),
                         lambda b: b["dimensions"]["tone"].update(score="bad"),
                         lambda b: b["dimensions"]["bias"].update(score=float("nan")),
                         lambda b: b["dimensions"]["accuracy"].update(status="unknown")):
            broken = json.loads(json.dumps(body))
            mutation(broken)
            with patch.object(wrapper, "analyze", return_value=broken):
                self.assertEqual(wrapper.decide("Hello", audit_log=False).decision, "HOLD_FOR_REVIEW")

    def test_audit_keeps_complete_mock_response(self):
        wrapper = importlib.import_module("wrapper")
        body, _ = mock_ezio.analyze("Hello", "")
        with patch.object(wrapper, "analyze", return_value=body), patch.object(wrapper.audit, "record") as record:
            result = wrapper.decide("Hello")
            self.assertEqual(result.ezio, body)
            record.assert_called_once()

    def test_audit_preserves_invalid_response_on_error(self):
        wrapper = importlib.import_module("wrapper")
        malformed = {"verdict": {"tier": "GREEN"}}
        with patch.object(wrapper, "analyze", return_value=malformed):
            result = wrapper.decide("Private ordinary input", audit_log=False)
        self.assertEqual(result.decision, "HOLD_FOR_REVIEW")
        self.assertEqual(result.ezio["response"], malformed)


class ExtraClaimsTests(unittest.TestCase):
    def test_extra_known_claims_extend_fact_checking(self):
        import tempfile
        claim = {"id": "extra_moon", "patterns": ["the moon is made of cheese"], "claim": "The moon is made of cheese.",
                 "verdict": "contradicted", "source": None}
        with tempfile.TemporaryDirectory() as directory, patch.dict(mock_ezio.KNOWN):
            path = Path(directory) / "extra.json"
            path.write_text(json.dumps({"known_claims": [claim]}))
            body, _ = mock_ezio.analyze("The moon is made of cheese.", "")
            self.assertEqual(body["dimensions"]["accuracy"]["status"], "not_scored")
            mock_ezio.load_extra_claims(path)
            body, _ = mock_ezio.analyze("The moon is made of cheese.", "")
            self.assertEqual(body["dimensions"]["accuracy"]["claims_contradicted"], 1)
            self.assertEqual(body["verdict"]["tier"], "RED")
            with self.assertRaises(ValueError):
                mock_ezio.load_extra_claims(path)
            path.write_text(json.dumps({"known_claims": [{**claim, "id": "extra_other", "patterns": ["Mixed Case"]}]}))
            with self.assertRaises(ValueError):
                mock_ezio.load_extra_claims(path)

    def test_public_claims_cover_practice_facts(self):
        for text, verdict in (("Yes, you can ask us to delete your personal data under the GDPR.", "supported"),
                              ("The GDPR only applies to companies headquartered in the EU.", "contradicted"),
                              ("The Fifth Amendment guarantees the right to a speedy trial.", "contradicted"),
                              ("The Sixth Amendment guarantees the right to a speedy and public trial.", "supported"),
                              ("The SEC sets the federal funds rate.", "contradicted"),
                              ("The Federal Open Market Committee sets the target range for the federal funds rate.", "supported")):
            with self.subTest(text=text):
                body, _ = mock_ezio.analyze(text, "")
                self.assertEqual([c["verdict"] for c in body["claims"]], [verdict])


class ClientTests(unittest.TestCase):
    def test_rejects_nonfinite_json(self):
        import io
        client = importlib.import_module("ezio_client")
        for raw in (b'{"score": NaN}', b'{"score": Infinity}', b'{"score": 1e999}'):
            with self.subTest(raw=raw), patch.object(client.urllib.request, "urlopen", return_value=io.BytesIO(raw)):
                with self.assertRaises(client.EzioError):
                    client.analyze("Hello", retries=0)

    def test_failed_http_response_is_available_for_audit(self):
        import io
        client = importlib.import_module("ezio_client")
        body = {"error": {"code": "service_unavailable"}}
        response = urllib.error.HTTPError("http://localhost/", 503, "unavailable", {"Retry-After": "0"},
                                          io.BytesIO(json.dumps(body).encode()))
        with patch.object(client.urllib.request, "urlopen", side_effect=response):
            with self.assertRaises(client.EzioError) as caught:
                client.analyze("Hello", retries=0)
        self.assertEqual(caught.exception.response, body)


if __name__ == "__main__":
    unittest.main()
