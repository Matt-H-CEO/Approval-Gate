"""HTTP client for the Ezio API. Done for you: auth, timeout, retry with backoff on 429 and 503."""
import http.client
import json
import math
import os
import time
import urllib.error
import urllib.request

BASE = os.getenv("EZIO_URL", "http://localhost:8787").rstrip("/")
KEY = os.getenv("EZIO_KEY", "mock-team-key")


class EzioError(Exception):
    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


def finite_number(raw):
    number = float(raw)
    if not math.isfinite(number):
        raise ValueError("Non-finite number in mock response")
    return number


def decode_response(raw):
    return json.loads(raw, parse_float=finite_number, parse_constant=finite_number)


def analyze(text, retries=3, timeout=15, scenario=None, budget=45):
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    if scenario:
        headers["X-Mock-Scenario"] = scenario
    body = json.dumps({"text": text, "depth": "simple"}).encode()
    deadline = time.monotonic() + budget
    last = "request budget exhausted"
    last_response = None
    for attempt in range(retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        delay = min(2 ** attempt, 4)
        req = urllib.request.Request(f"{BASE}/v1/analyze", data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=min(timeout, remaining)) as resp:
                raw = resp.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise EzioError("mock response exceeds 1000000 bytes")
                return decode_response(raw)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            try:
                error_body = e.read(1_000_001)
                response = decode_response(error_body) if len(error_body) <= 1_000_000 else {"error": "response exceeds limit"}
            except (OSError, ValueError):
                response = None
            last_response = response
            if e.code not in (429, 503):
                e.close()
                raise EzioError(last, response) from e
            try:
                requested = float(e.headers.get("Retry-After", delay))
                delay = min(max(requested, 0), 4) if math.isfinite(requested) else delay
            except ValueError:
                pass
            finally:
                e.close()
        except (OSError, http.client.HTTPException, ValueError) as e:
            last = type(e).__name__
            last_response = None
        if attempt == retries:
            break
        time.sleep(min(delay, max(0, deadline - time.monotonic())))
    raise EzioError(last, last_response)
