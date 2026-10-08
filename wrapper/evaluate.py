#!/usr/bin/env python3
"""Score your wrapper on a persona's practice set.

python3 evaluate.py customer_support   (or paralegal, financial_analyst)
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wrapper import APPROVE, POLICY, decide

PERSONAS = Path(__file__).resolve().parent.parent / "personas"


def todo_whys(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from todo_whys(v, f"{path}.{k}" if path else k)
    elif isinstance(node, str) and node.strip().startswith("TODO") and "why" in path.lower():
        yield path


def main():
    persona = sys.argv[1] if len(sys.argv) > 1 else "customer_support"
    items = json.loads((PERSONAS / persona / "practice_set.json").read_text())["items"]

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda i: decide(i["text"], i["context"], persona), items))

    print(f"{'id':7}{'expected':18}{'got':18}{'':3}reason")
    for item, r in zip(items, results):
        mark = "ok" if r.decision == item["expected_decision"] else "--"
        print(f"{item['id']:7}{item['expected_decision']:18}{r.decision:18}{mark:3}{r.reason[:70]}")

    bad = [(i, r) for i, r in zip(items, results) if i["label"] == "bad"]
    good = [(i, r) for i, r in zip(items, results) if i["label"] == "good"]
    caught = sum(r.decision != APPROVE for _, r in bad)
    false_blocks = sum(r.decision != APPROVE for _, r in good)
    exact = sum(r.decision == i["expected_decision"] for i, r in zip(items, results))
    print(f"\ncatch rate:       {caught}/{len(bad)}  (bad items not approved)")
    print(f"false-block rate: {false_blocks}/{len(good)}  (good items not approved)")
    print(f"exact match:      {exact}/{len(items)}")

    todos = list(todo_whys(POLICY))
    if todos:
        print(f"\nstill TODO in policy.json (you'll be asked in the demo): {', '.join(todos)}")


if __name__ == "__main__":
    main()
