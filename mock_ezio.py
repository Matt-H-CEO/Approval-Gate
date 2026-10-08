#!/usr/bin/env python3
"""Mock Ezio API for the buildathon. Standard library only.

Run:  python3 mock_ezio.py
Env:  PORT (8787)  HOST (127.0.0.1)  MOCK_API_KEY (mock-team-key)
      LATENCY ("2-6" seconds, "0" to disable)  CHAOS (0-100, % of random 429/503)
      EXTRA_KNOWN_CLAIMS (path to a JSON file of additional fact-check claims; used by the judge mock)
"""
import hashlib
import hmac
import json
import os
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = json.loads((HERE / "fixtures.json").read_text())

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8787"))
API_KEY = os.getenv("MOCK_API_KEY", "mock-team-key")
ADMIN = os.getenv("ADMIN", "0") == "1"
ADMIN_KEY = os.getenv("MOCK_ADMIN_KEY", "")
CHAOS = float(os.getenv("CHAOS", "0"))
SCENARIOS = {"slow", "rate_limited", "server_error", "down", "malformed", "unavailable"}
MAX_TEXT = 20000


def parse_latency(value):
    parts = [float(p) for p in value.split("-")]
    return parts[0], parts[-1]


LAT_MIN, LAT_MAX = parse_latency(os.getenv("LATENCY", "2-6"))
GREEN_MIN = DATA["tier_bands"]["green_min"]
YELLOW_MIN = DATA["tier_bands"]["yellow_min"]
KNOWN = {c["id"]: c for c in DATA["known_claims"]}


def load_extra_claims(path):
    extra = json.loads(Path(path).read_text())["known_claims"]
    reused = [c["id"] for c in extra if c["id"] in KNOWN]
    if reused:
        raise ValueError(f"Extra known claims reuse existing ids: {', '.join(reused)}")
    uppercase = [p for c in extra for p in c["patterns"] if p != p.lower()]
    if uppercase:
        raise ValueError(f"Extra claim patterns must be lowercase: {', '.join(uppercase)}")
    KNOWN.update({c["id"]: c for c in extra})


if os.getenv("EXTRA_KNOWN_CLAIMS"):
    load_extra_claims(os.environ["EXTRA_KNOWN_CLAIMS"])


def norm(text):
    return re.sub(r"\s+", " ", text.strip().lower()).rstrip(". ")


FIXTURE_INDEX = {norm(t): f for f in DATA["fixtures"] for t in f["texts"]}

HYPE = re.compile(r"\b(amazing|incredible|guaranteed|best|greatest|unbelievable|life-changing|revolutionary)\b", re.I)
ONE_SIDED = re.compile(r"\b(clearly|obviously|everyone knows|nobody|no one|undeniabl\w*|anyone who disagrees|only (?:answer|way)|idiot|without a doubt)\b")
SYCOPHANCY = re.compile(r"\b(great question|you'?re absolutely right|you are absolutely right|excellent point|as you said|you'?re right|good point|brilliant)\b")
HEDGE = re.compile(r"\b(perhaps|might|could|arguably|seems?|somewhat|in general|tend to|maybe|possibly)\b")

WEIGHTS = {"accuracy": 0.35, "bias": 0.25, "tone": 0.15, "agreement": 0.25}
CLAIM_POINTS = {"supported": 100, "unverifiable": 50, "contradicted": 0}
DIM_TEXT = {
    "tone": ("Tone", "Tone is neutral and objective.", "Tone is emotional or promotional."),
    "bias": ("Bias", "Framing is balanced.", "Framing is one-sided."),
    "agreement": ("Agreement", "Response reads as independent.", "Response flatters or over-agrees with the user."),
}
ISSUES = {"tone": "emotional tone", "bias": "one-sided framing", "agreement": "excessive agreement"}

_seen = set()
_lock = threading.Lock()


def clamp(value, low=0, high=100):
    return max(low, min(high, value))


def tier_of(score):
    return "GREEN" if score >= GREEN_MIN else "YELLOW" if score >= YELLOW_MIN else "RED"


def severity_of(score):
    return "NOTE" if score >= 80 else "WARNING" if score >= 60 else "CRITICAL"


def resolve_claim(item):
    if isinstance(item, str):
        item = KNOWN[item]
    return {"text": item["claim"], "verdict": item["verdict"], "source": item.get("source")}


def features_from_fixture(fixture):
    claims = [resolve_claim(c) for c in fixture["claims"]]
    status = fixture.get("accuracy_status") or ("scored" if claims else "not_scored")
    return {
        "bias": fixture["bias"], "tone": fixture["tone"], "agreement": fixture["agreement"],
        "claims": claims, "accuracy_status": status, "low_info": fixture.get("low_info", False),
    }


def features_from_text(text):
    n = norm(text)
    tone = clamp(
        100
        - 10 * min(text.count("!"), 5)
        - 6 * min(len(re.findall(r"\b[A-Z]{3,}\b", text)), 5)
        - 6 * min(len(HYPE.findall(text)), 4),
        20,
    )
    bias = clamp(100 - 15 * len(ONE_SIDED.findall(n)), 25)
    agreement = clamp(100 - 25 * len(SYCOPHANCY.findall(n)), 20)
    claims = [resolve_claim(c) for c in KNOWN.values() if any(p in n for p in c["patterns"])]
    return {
        "bias": bias, "tone": tone, "agreement": agreement, "claims": claims,
        "accuracy_status": "scored" if claims else "not_scored",
        "low_info": len(HEDGE.findall(n)) >= 4,
    }


def composite(feat, accuracy_score, contradicted):
    parts = {k: feat[k] for k in ("bias", "tone", "agreement")}
    if accuracy_score is not None:
        parts["accuracy"] = accuracy_score
    score = sum(WEIGHTS[k] * v for k, v in parts.items()) / sum(WEIGHTS[k] for k in parts)
    if feat["low_info"]:
        score -= 15
    if contradicted:
        score = min(score, 45)
    return round(clamp(score), 1)


def build(feat, score=None):
    status = feat["accuracy_status"]
    claims = feat["claims"] if status == "scored" else []
    total = len(claims)
    supported = sum(c["verdict"] == "supported" for c in claims)
    contradicted = sum(c["verdict"] == "contradicted" for c in claims)
    accuracy_score = round(sum(CLAIM_POINTS[c["verdict"]] for c in claims) / total, 1) if total else None
    if score is None:
        score = composite(feat, accuracy_score, contradicted)

    findings = []
    if status == "unavailable":
        findings.append({"severity": "WARNING", "dimension": "accuracy", "title": "Factual Claim Analysis",
                         "body": "The fact-check service was unavailable, so accuracy could not be checked."})
    elif status == "not_scored":
        findings.append({"severity": "N/A", "dimension": "accuracy", "title": "Factual Claim Analysis",
                         "body": "No verifiable claims were extracted, so accuracy is not scored."})
    elif contradicted:
        findings.append({"severity": "CRITICAL", "dimension": "accuracy", "title": "Factual Claim Analysis",
                         "body": f"{contradicted} of {total} claims contradicted by sources."})
    elif supported < total:
        findings.append({"severity": "WARNING", "dimension": "accuracy", "title": "Factual Claim Analysis",
                         "body": f"{total - supported} of {total} claims could not be verified."})
    else:
        findings.append({"severity": "NOTE", "dimension": "accuracy", "title": "Factual Claim Analysis",
                         "body": f"All {total} claims are supported by sources."})
    for key, (title, good, bad) in DIM_TEXT.items():
        findings.append({"severity": severity_of(feat[key]), "dimension": key, "title": title,
                         "body": good if feat[key] >= 80 else f"{bad} (score {feat[key]})."})
    if feat["low_info"]:
        findings.append({"severity": "WARNING", "dimension": "overall", "title": "Low information",
                         "body": "Heavy hedging and filler; little substance."})

    issues = [ISSUES[k] for k in ("tone", "bias", "agreement") if feat[k] < 80]
    if contradicted:
        issues.insert(0, "a claim is contradicted by sources")
    elif status == "scored" and supported < total:
        issues.append("some claims could not be verified")
    if status == "unavailable":
        issues.append("accuracy could not be checked")
    if feat["low_info"]:
        issues.append("little substance")

    tier = tier_of(score)
    detail = ", ".join(issues)
    if tier == "GREEN":
        if issues:
            summary = f"Looks reliable, but note: {detail}."
        elif status == "scored":
            summary = "Well-sourced and balanced."
        else:
            summary = "Neutral and balanced. No claims were verified."
        label = "Trustworthy"
    elif tier == "YELLOW":
        summary = f"Use with care: {detail}." if issues else "Use with care: moderate concerns across dimensions."
        label = "Use With Care"
    else:
        summary = f"Not reliable: {detail}." if issues else "Not reliable: low scores across dimensions."
        label = "Not Reliable"

    return {
        "verdict": {"tier": tier, "label": label, "score": score, "summary": summary},
        "dimensions": {
            "accuracy": {"status": status, "score": accuracy_score, "claims_total": total,
                         "claims_verified": supported, "claims_contradicted": contradicted},
            "bias": {"score": feat["bias"]},
            "tone": {"score": feat["tone"]},
            "agreement": {"score": feat["agreement"]},
        },
        "claims": claims,
        "findings": findings,
    }


def analyze(text, scenario):
    fixture = FIXTURE_INDEX.get(norm(text))
    feat = features_from_fixture(fixture) if fixture else features_from_text(text)
    score = fixture["score"] if fixture else None
    if scenario == "unavailable":
        feat = {**feat, "accuracy_status": "unavailable", "claims": []}
        score = None
    source = f"fixture:{fixture['id']}" if fixture else "generated"
    return build(feat, score), source


class Handler(BaseHTTPRequestHandler):
    server_version = "MockEzio/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, headers=None, source="-", delay=0.0):
        raw = json.dumps(body, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            return
        print(f"{self.command} {self.path} -> {code} [{source}] {delay:.1f}s", flush=True)

    def _error(self, code, name, message, headers=None):
        self._send(code, {"error": {"code": name, "message": message}}, headers)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Mock-Scenario")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        if self.path == "/v1/health":
            self._send(200, {"status": "ok", "mock": True})
        elif self.path == "/":
            self._send(200, {"service": "mock-ezio", "endpoints": ["POST /v1/analyze", "GET /v1/health"]})
        else:
            self._error(404, "not_found", "Unknown endpoint.")

    def do_POST(self):
        admin_request = ADMIN and self.path == "/admin/mode"
        if self.path != "/v1/analyze" and not admin_request:
            return self._error(404, "not_found", "Unknown endpoint.")
        key = ADMIN_KEY if admin_request else API_KEY
        if not key or not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {key}"):
            return self._error(401, "unauthorized", "Missing or invalid API key.")
        self.connection.settimeout(10)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 1_000_000:
                return self._error(400, "invalid_request", "Body must be 1 to 1000000 bytes.")
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError, OSError):
            return self._error(400, "invalid_request", "Body must be valid JSON.")
        if admin_request:
            mode = payload.get("mode") if isinstance(payload, dict) else None
            if not isinstance(mode, str) or mode not in SCENARIOS | {"none"}:
                return self._error(400, "invalid_request", "Unknown judge mode.")
            with _lock:
                self.server.judge_mode = mode
                _seen.clear()
            return self._send(200, {"mode": mode})
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            return self._error(400, "invalid_request", "'text' must be a non-empty string.")
        if len(text) > MAX_TEXT:
            return self._error(400, "invalid_request", f"'text' exceeds {MAX_TEXT} characters.")

        with _lock:
            mode = getattr(self.server, "judge_mode", "none")
        scenario = ("" if mode == "none" else mode) if ADMIN else (self.headers.get("X-Mock-Scenario") or "").strip().lower()
        if scenario and scenario not in SCENARIOS:
            return self._error(400, "invalid_scenario", f"X-Mock-Scenario must be one of: {', '.join(sorted(SCENARIOS))}.")

        digest = hashlib.sha1(norm(text).encode()).hexdigest()
        if scenario == "down":
            return self._error(503, "service_unavailable", "Service unavailable.", {"Retry-After": "1"})
        if scenario in ("rate_limited", "server_error"):
            key = (scenario, digest)
            with _lock:
                first = key not in _seen
                _seen.add(key)
            if first and scenario == "rate_limited":
                return self._error(429, "rate_limited", "Too many requests.", {"Retry-After": "2"})
            if first:
                return self._error(503, "service_unavailable", "Service unavailable.", {"Retry-After": "1"})
        elif not scenario and CHAOS and random.random() * 100 < CHAOS:
            if random.random() < 0.5:
                return self._error(429, "rate_limited", "Too many requests.", {"Retry-After": "2"})
            return self._error(503, "service_unavailable", "Service unavailable.", {"Retry-After": "1"})

        body, source = analyze(text, scenario)
        if scenario == "slow":
            delay = 8.0
        else:
            delay = LAT_MIN + (int(digest[:6], 16) % 1000) / 1000 * (LAT_MAX - LAT_MIN)
        time.sleep(delay)

        response = {
            "request_id": f"mock_{digest[:6]}",
            "status": "completed",
            "depth": payload.get("depth", "simple"),
            **body,
            "processing_time_ms": int(delay * 1000),
        }
        if scenario == "malformed":
            del response["dimensions"]
        self._send(200, response, {"X-Mock-Source": source}, source, delay)


def main():
    if ADMIN and (not ADMIN_KEY or ADMIN_KEY == API_KEY):
        raise SystemExit("Judge mode requires a separate MOCK_ADMIN_KEY; do not share it with teams.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Approval Gate mock on http://{HOST}:{PORT}  latency={LAT_MIN}-{LAT_MAX}s  chaos={CHAOS}%  judge_mode={ADMIN}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
