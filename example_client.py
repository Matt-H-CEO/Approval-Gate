#!/usr/bin/env python3
"""Smallest useful client: auth, timeout, retry on 429/503, and the accuracy-state check."""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("EZIO_URL", "http://localhost:8787")
KEY = os.getenv("EZIO_KEY", "mock-team-key")


def analyze(text, retries=3, timeout=15, scenario=None):
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    if scenario:
        headers["X-Mock-Scenario"] = scenario
    body = json.dumps({"text": text, "depth": "simple"}).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(f"{BASE}/v1/analyze", data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < retries:
                time.sleep(float(e.headers.get("Retry-After", 2 ** attempt)))
                continue
            raise
        except (TimeoutError, urllib.error.URLError):
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise


if __name__ == "__main__":
    text = " ".join(sys.argv[1:]) or "Your 30% loyalty discount applies to the Pro plan."
    result = analyze(text)
    verdict, accuracy = result["verdict"], result["dimensions"]["accuracy"]
    print(f"{verdict['tier']} {verdict['score']}  {verdict['summary']}")
    print(f"accuracy: {accuracy['status']}")
    if accuracy["status"] != "scored":
        print("WARNING: claims were not checked. GREEN here does not mean the text is accurate.")
