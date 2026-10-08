#!/usr/bin/env python3
"""Self-contained test: starts the mock on a free port, checks the contract, stops it."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = json.loads((HERE / "fixtures.json").read_text())
KEY = "mock-team-key"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"


def call(text, scenario=None, key=KEY, path="/v1/analyze", raw_body=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if scenario:
        headers["X-Mock-Scenario"] = scenario
    data = raw_body if raw_body is not None else json.dumps({"text": text}).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.load(e), e.headers


def tier(score):
    b = DATA["tier_bands"]
    return "GREEN" if score >= b["green_min"] else "YELLOW" if score >= b["yellow_min"] else "RED"


results = []


def check(name, condition):
    results.append(condition)
    print(f"{'ok  ' if condition else 'FAIL'} {name}")


def main():
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "mock_ezio.py")],
        env={**os.environ, "PORT": str(PORT), "LATENCY": "0", "CHAOS": "0"},
        stdout=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(BASE + "/v1/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)

        code, body, _ = call("x", path="/v1/health", raw_body=b"")
        check("health responds", urllib.request.urlopen(BASE + "/v1/health").status == 200)
        check("missing key -> 401", call("hi", key=None)[0] == 401)
        check("wrong key -> 401", call("hi", key="nope")[0] == 401)
        check("empty text -> 400", call("   ")[0] == 400)
        check("bad json -> 400", call("", raw_body=b"{not json")[0] == 400)
        check("unknown scenario -> 400", call("hi", scenario="bogus")[0] == 400)

        for f in DATA["fixtures"]:
            for text in f["texts"]:
                code, body, headers = call(text)
                v = body["verdict"]
                check(
                    f"fixture {f['id']} {f['name']}: {v['tier']} {v['score']}",
                    code == 200 and v["score"] == f["score"] and v["tier"] == tier(f["score"])
                    and headers["X-Mock-Source"] == f"fixture:{f['id']}",
                )

        _, b1, _ = call("America has 50 states.")
        check("trailing period still matches fixture 2", b1["verdict"]["score"] == 96.0)
        _, b4, _ = call("The earth is flat.")
        check("fixture 4 has contradicted claim", b4["dimensions"]["accuracy"]["claims_contradicted"] == 1)
        _, b3, _ = call("Your 30% loyalty discount applies to the Pro plan.")
        check("fixture 3 is the trap: GREEN + not_scored",
              b3["verdict"]["tier"] == "GREEN" and b3["dimensions"]["accuracy"]["status"] == "not_scored")

        unseen = "The quarterly report will be sent on Friday."
        _, a, _ = call(unseen)
        _, b, _ = call(unseen)
        check("unseen text is deterministic", a == b)
        check("unseen neutral text is GREEN, not_scored",
              a["verdict"]["tier"] == "GREEN" and a["dimensions"]["accuracy"]["status"] == "not_scored")
        _, loud, _ = call("This is INCREDIBLE!!! You will LOVE it, guaranteed!!!")
        check("unseen promotional text lowers tone", loud["dimensions"]["tone"]["score"] < 80)
        _, known, _ = call("Note that water boils at 50 degrees Celsius at sea level.")
        check("unseen text with known false claim is RED", known["verdict"]["tier"] == "RED")

        code, _, h = call("rate limit probe", scenario="rate_limited")
        check("rate_limited first call -> 429 + Retry-After", code == 429 and h["Retry-After"] == "2")
        check("rate_limited retry succeeds", call("rate limit probe", scenario="rate_limited")[0] == 200)
        check("server_error first call -> 503", call("server probe", scenario="server_error")[0] == 503)
        check("server_error retry succeeds", call("server probe", scenario="server_error")[0] == 200)
        check("down always 503", call("any", scenario="down")[0] == 503 and call("any", scenario="down")[0] == 503)
        code, bm, _ = call("malformed probe", scenario="malformed")
        check("malformed drops dimensions", code == 200 and "dimensions" not in bm)
        _, un, _ = call("America has 50 states", scenario="unavailable")
        check("unavailable overrides accuracy status", un["dimensions"]["accuracy"]["status"] == "unavailable")
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
