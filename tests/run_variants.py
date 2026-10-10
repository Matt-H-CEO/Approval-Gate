#!/usr/bin/env python3
"""Run the team-written variants (tests/variants.json) through a running gate.

These are not from the practice set. We wrote both the checker and these inputs,
so this is a generalization smoke test, not an independent score.

Usage:
    python3 gate.py serve --persona customer_support      # in one terminal
    python3 tests/run_variants.py http://127.0.0.1:<port> # in another

A 'good' item should be APPROVE. A 'bad' item should be anything but APPROVE
(HOLD_FOR_REVIEW or BLOCK) - the same convention the practice set uses for
"caught" and "false blocks".
"""
import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PERSONA = "customer_support"
TIMEOUT = 90


def decide(base, item):
    body = json.dumps({"text": item["text"], "persona": PERSONA,
                       "context": item.get("context") or {}}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/decide", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            out = json.load(r)
        return dict(item, decision=out.get("decision"), reason=out.get("reason", ""))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return dict(item, decision="ERROR", reason=f"{type(e).__name__}: {e}")


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python3 tests/run_variants.py http://127.0.0.1:<wrapper port>")
    base = sys.argv[1]
    items = json.loads((Path(__file__).parent / "variants.json").read_text())["items"]
    good = [i for i in items if i["label"] == "good"]
    bad = [i for i in items if i["label"] == "bad"]
    print(f"{len(items)} variants: {len(good)} good, {len(bad)} bad -> {base}\n")

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda i: decide(base, i), items))

    approved = [r for r in rows if r["decision"] == "APPROVE"]
    caught = [r for r in rows if r["label"] == "bad" and r["decision"] not in ("APPROVE", "ERROR")]
    false_blocks = [r for r in rows if r["label"] == "good" and r["decision"] not in ("APPROVE", "ERROR")]
    errors = [r for r in rows if r["decision"] == "ERROR"]

    for r in sorted(rows, key=lambda x: x["id"]):
        ok = (r["decision"] == "APPROVE") if r["label"] == "good" else (r["decision"] not in ("APPROVE", "ERROR"))
        print(f"  {'ok  ' if ok else 'MISS'} {r['id']:<8} {r['label']:<5} -> {r['decision']}")
        if not ok:
            print(f"         {r['reason'][:150]}")

    print(f"\nbad caught        {len(caught)}/{len(bad)}")
    print(f"good approved     {len(good) - len(false_blocks)}/{len(good)}")
    print(f"false blocks      {len(false_blocks)}/{len(good)}")
    if errors:
        print(f"errors            {len(errors)} (gate unreachable or malformed)")
    clean = len(caught) == len(bad) and not false_blocks and not errors
    print("\n" + ("all 40 variants behaved as labelled." if clean else "see MISS lines above."))
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
