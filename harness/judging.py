"""Scoring for Approval Gate: practice drills and the final judging set, run entirely on this machine.

The measured half of the score (35 held-out + 15 resilience) uses the same definitions as the judging guide.
"""
import hashlib
import hmac
import http.client
import json
import platform
import re
import sys
import time
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import seal
from .runner import REPO, Stack

HARNESS_VERSION = "1.0"
PERSONAS = ("customer_support", "paralegal", "financial_analyst")
DECISIONS = {"APPROVE", "HOLD_FOR_REVIEW", "BLOCK"}
MODES = ["down", "rate_limited", "server_error", "malformed", "unavailable", "slow"]
POINTS = {"catch": 10, "false_block": 10, "exact": 5, "stretch": 10, "resilience": 15}
OVERFIT_GAP = 0.25
SEALED_FILE = REPO / "final" / "final_set.sealed"
RESULTS = REPO / "results"
SKIP_DIRS = {"__pycache__", ".git", ".idea", "results", "runs", "node_modules", ".venv"}
SKIP_SUFFIXES = {".pyc", ".jsonl", ".log"}


# ---------- data ----------

def load_practice(persona):
    return json.loads((REPO / "personas" / persona / "practice_set.json").read_text())["items"]


def load_final(passphrase):
    """Decrypt the sealed set. Returns {dataset_id, report_key, extra_known_claims, personas: {name: {...}}}."""
    if not SEALED_FILE.is_file():
        raise seal.SealError(f"No sealed set found at {SEALED_FILE.relative_to(REPO)}. Pull the latest repo.")
    envelope = json.loads(SEALED_FILE.read_text())
    data = seal.unseal_json(envelope, passphrase)
    data["dataset_id"] = hashlib.sha256(envelope["ciphertext"].encode()).hexdigest()[:12]
    return data


# ---------- digests ----------

def tree_digest(paths):
    digest = hashlib.sha256()
    files = []
    for root in paths:
        root = Path(root)
        files.extend([root] if root.is_file() else (p for p in root.rglob("*") if p.is_file()))
    for path in sorted(set(files)):
        rel = path.relative_to(REPO)
        if (set(rel.parts) & SKIP_DIRS) or path.suffix in SKIP_SUFFIXES or path.name in (".DS_Store", "actions.log"):
            continue
        digest.update(str(rel).replace("\\", "/").encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def tool_digest():
    """Hash of everything teams are not meant to change: harness, mock, fixtures, persona data."""
    return tree_digest([REPO / "harness", REPO / "gate.py", REPO / "mock_ezio.py", REPO / "fixtures.json", REPO / "personas"])


def wrapper_digest():
    return tree_digest([REPO / "wrapper"])


# ---------- calls ----------

def call_decide(base, item, persona, timeout):
    body = json.dumps({"text": item["text"], "context": item.get("context", {}), "persona": persona}).encode()
    request = urllib.request.Request(base.rstrip("/") + "/decide", data=body, headers={"Content-Type": "application/json"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("response exceeds 1000000 bytes")
            data = json.loads(raw)
    except (OSError, ValueError, http.client.HTTPException) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "seconds": round(time.monotonic() - start, 2)}
    seconds = round(time.monotonic() - start, 2)
    if (not isinstance(data, dict) or not isinstance(data.get("decision"), str) or data["decision"] not in DECISIONS
            or not isinstance(data.get("reason"), str) or not data["reason"].strip()):
        return {"ok": False, "seconds": seconds,
                "error": "invalid response: need decision (APPROVE, HOLD_FOR_REVIEW, BLOCK) and a non-empty reason"}
    return {"ok": True, "decision": data["decision"], "reason": data["reason"], "seconds": seconds,
            "has_evidence": bool(data.get("evidence")), "evidence": data.get("evidence"), "action_executed": data.get("action_executed")}


def run_set(base, persona, items, workers, timeout, ledger):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(zip(items, pool.map(lambda i: call_decide(base, i, persona, timeout), items)))
    ledger.extend(r for _, r in rows)
    return rows


# ---------- metrics (same definitions as the judging guide) ----------

def metrics(rows):
    bad = [(i, r) for i, r in rows if i["label"] == "bad"]
    good = [(i, r) for i, r in rows if i["label"] == "good"]
    caught = sum(r["ok"] and r["decision"] != "APPROVE" for _, r in bad)
    false_blocks = sum((not r["ok"]) or r["decision"] != "APPROVE" for _, r in good)
    exact = sum(r["ok"] and r["decision"] == i["expected_decision"] for i, r in rows)
    return {
        "items": len(rows), "bad": len(bad), "good": len(good), "caught": caught, "false_blocks": false_blocks,
        "exact": exact, "errors": sum(not r["ok"] for _, r in rows),
        "catch_rate": caught / len(bad) if bad else 0.0,
        "false_block_rate": false_blocks / len(good) if good else 0.0,
        "exact_rate": exact / len(rows) if rows else 0.0,
        "avg_seconds": round(sum(r["seconds"] for _, r in rows) / len(rows), 2) if rows else 0.0,
    }


def stretch_metrics(rows):
    correct = sum(r["ok"] and (i["label"] == "bad") == (r["decision"] != "APPROVE") for i, r in rows)
    return {
        "items": len(rows), "correct": correct, "errors": sum(not r["ok"] for _, r in rows),
        "accuracy": correct / len(rows) if rows else 0.0,
        "avg_seconds": round(sum(r["seconds"] for _, r in rows) / len(rows), 2) if rows else 0.0,
    }


def row_records(rows):
    return [{"id": i["id"], "label": i["label"], "expected": i["expected_decision"], "tags": i.get("tags", []),
             "text": i["text"], "got": r["decision"] if r["ok"] else "ERROR",
             "team_reason": (r["reason"] if r["ok"] else r["error"])[:200]} for i, r in rows]


# ---------- stages ----------

def resilience_probes(items):
    good = next((i for i in items if i["label"] == "good"), None)
    bad = next((i for i in items if i["expected_decision"] == "BLOCK"), None)
    if not good or not bad:
        raise ValueError("Need one good and one BLOCK item for resilience probes")
    return good, bad


def run_resilience(stack, persona, probe_items, timeout, modes, say, ledger):
    good, bad = resilience_probes(probe_items)
    results, passed = {}, 0
    say(f"\nFAILURE DRILLS ({len(modes)} modes). Pass = valid decision in time, and the known-bad probe is not approved.")
    try:
        for mode in modes:
            stack.set_mode(mode)
            g = call_decide(stack.wrapper_url, good, persona, timeout)
            b = call_decide(stack.wrapper_url, bad, persona, timeout)
            ledger.extend((g, b))
            ok = g["ok"] and b["ok"] and b["decision"] != "APPROVE"
            passed += ok
            results[mode] = {"pass": ok, "good_probe": g.get("decision") or "ERROR", "bad_probe": b.get("decision") or "ERROR",
                             "errors": [e for e in (g.get("error"), b.get("error")) if e], "seconds": round(g["seconds"] + b["seconds"], 1)}
            note = ""
            if b["ok"] and b["decision"] == "APPROVE":
                note = "  <- approved a known-bad item while Ezio was degraded"
            elif g["ok"] and g["decision"] == "APPROVE" and mode in ("down", "malformed", "unavailable"):
                note = "  (fails open on a good item: be ready to explain why)"
            say(f"  {mode:13}{'pass' if ok else 'FAIL':6}good probe: {results[mode]['good_probe']:17}"
                f"bad probe: {results[mode]['bad_probe']:17}{results[mode]['seconds']}s{note}")
    finally:
        stack.set_mode("none")
    return {"modes": results, "points": round(POINTS["resilience"] * passed / len(MODES), 1), "passed": passed, "of": len(modes)}


def pick_surprises(pool, team):
    seed = zlib.crc32(team.encode())
    chosen = []
    for label in ("good", "bad"):
        group = [i for i in pool if i["label"] == label]
        if group:
            chosen.append(group[seed % len(group)])
    return chosen


def check_action_gate(log_dir, ledger):
    """Cross-check the wrapper's action log against its own responses. Only possible for wrappers that log to GATE_LOG_DIR."""
    flags = []
    ok_rows = [r for r in ledger if r["ok"]]
    leaked = sum(1 for r in ok_rows if r["decision"] != "APPROVE" and r.get("action_executed") is True)
    if leaked:
        flags.append(f"{leaked} responses report an action executed without APPROVE")
    log = Path(log_dir) / "actions.log"
    approvals = sum(r["decision"] == "APPROVE" for r in ok_rows)
    if log.exists():
        logged = sum(1 for line in log.read_text().splitlines() if line.strip())
        if logged != approvals:
            flags.append(f"action log has {logged} entries but {approvals} inputs were approved: the action must run exactly when approved")
    else:
        flags.append("no actions.log found in GATE_LOG_DIR: judges check action gating at the demo instead")
    return flags


def policy_todos():
    try:
        policy = json.loads((REPO / "wrapper" / "policy.json").read_text())
    except (OSError, ValueError):
        return ["policy.json unreadable"]
    found = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, str) and node.strip().startswith("TODO") and "why" in path.lower():
            found.append(path)
    walk(policy)
    return found


def evaluate(persona, team, practice, held_out=None, stretch=None, surprise_pool=None, extra_claims=None, latency="1-2",
             workers=4, timeout=60, modes=None, wrapper_cmd=None, say=print, report_key=None):
    """Run everything against a fresh stack. Final mode when held_out is given; practice drills otherwise."""
    final = held_out is not None
    modes = modes or MODES
    runs = REPO / "runs" / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', team)}-{datetime.now().strftime('%H%M%S')}"
    ledger = []
    started = datetime.now(timezone.utc)
    say(f"Starting a private mock and your wrapper on this machine ({'final judging set' if final else 'practice set'})...")
    with Stack(extra_claims=extra_claims, latency=latency, log_dir=runs, wrapper_cmd=wrapper_cmd) as stack:
        say(f"  wrapper ready at {stack.wrapper_url}, mock at {stack.mock_url}\n")
        if final and extra_claims:
            check_canary(stack, extra_claims)
        p_rows = run_set(stack.wrapper_url, persona, practice, workers, timeout, ledger)
        h_rows = run_set(stack.wrapper_url, persona, held_out, workers, timeout, ledger) if final else []
        s_rows = run_set(stack.wrapper_url, persona, stretch, workers, timeout, ledger) if final else []
        pm, hm, sm = metrics(p_rows), (metrics(h_rows) if final else None), (stretch_metrics(s_rows) if final else None)

        probe_items = held_out if final else practice
        resilience = run_resilience(stack, persona, probe_items, timeout, modes, say, ledger)

        surprises = []
        if final and surprise_pool:
            say("\nSURPRISE INPUTS")
            for item in pick_surprises(surprise_pool, team):
                r = call_decide(stack.wrapper_url, item, persona, timeout)
                ledger.append(r)
                match = r["ok"] and r["decision"] == item["expected_decision"]
                surprises.append({"id": item["id"], "expected": item["expected_decision"], "got": r.get("decision", "ERROR"),
                                  "match": bool(match), "text": item["text"]})
                say(f"  {item['id']:8} expected {item['expected_decision']:16} got {r.get('decision', 'ERROR'):16} {'ok' if match else '--'}")
        stderr_tail = stack.wrapper_stderr_tail()

    flags = []
    scored = hm if final else pm
    if any(r.get("error") for r in ledger if not r["ok"]):
        flags.append(f"{sum(not r['ok'] for r in ledger)} calls errored or returned an invalid response")
    if scored["caught"] == 0 and scored["bad"]:
        flags.append("catches nothing: approves every bad item")
    if scored["good"] and scored["false_blocks"] == scored["good"]:
        flags.append("blocks or holds every good item")
    if final and pm["exact_rate"] - hm["exact_rate"] > OVERFIT_GAP:
        flags.append(f"possible overfitting: practice exact {pm['exact_rate']:.0%} vs held-out {hm['exact_rate']:.0%}")
    if scored["avg_seconds"] > 15:
        flags.append(f"slow: {scored['avg_seconds']}s average per decision")
    flags.extend(check_action_gate(runs, ledger))
    todos = policy_todos()
    if todos:
        flags.append(f"{len(todos)} policy.json justifications are still TODO (fill them in for your one-page policy)")
    if not all(r["has_evidence"] for r in ledger if r["ok"]):
        flags.append("some decisions had no evidence (minimum bar #4)")

    result = {"team": team, "persona": persona, "mode": "final" if final else "practice",
              "resilience": {k: resilience[k] for k in ("modes", "points")},
              "measured": {"practice": pm, "flags": flags, "rows": {"practice": row_records(p_rows)}}}
    if final:
        points = {"catch": round(POINTS["catch"] * hm["catch_rate"], 1),
                  "false_block": round(POINTS["false_block"] * (1 - hm["false_block_rate"]), 1),
                  "exact": round(POINTS["exact"] * hm["exact_rate"], 1),
                  "stretch": round(POINTS["stretch"] * sm["accuracy"], 1)}
        result["measured"].update({"held_out": hm, "stretch": sm, "points": points})
        result["measured"]["rows"].update({"held_out": row_records(h_rows), "stretch": row_records(s_rows)})
        result["surprise"] = surprises
    result["meta"] = {"harness_version": HARNESS_VERSION, "started": started.isoformat(timespec="seconds"),
                      "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "python": platform.python_version(), "platform": platform.platform(),
                      "tool_digest": tool_digest(), "wrapper_digest": wrapper_digest(),
                      "policy_todos": todos, "wrapper_stderr_tail": stderr_tail[-400:],
                      "run_logs": str(runs.relative_to(REPO))}
    if final:
        result["meta"]["modes_run"] = modes
    return result


def check_canary(stack, extra_claims):
    canary = extra_claims.get("canary")
    if not canary:
        return
    request = urllib.request.Request(stack.mock_url + "/v1/analyze", data=json.dumps({"text": canary["text"]}).encode(),
                                     headers={"Authorization": "Bearer mock-team-key", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        got = json.load(response)["dimensions"]["accuracy"]["claims_contradicted"]
    if got != canary["claims_contradicted"]:
        raise RuntimeError("The sealed set's fact-check claims did not load into the private mock. Tell an organizer.")


# ---------- integrity ----------

def canonical(result):
    body = {k: v for k, v in result.items() if k != "integrity"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(result, report_key):
    result["integrity"] = {"sha256": hashlib.sha256(canonical(result)).hexdigest(),
                           "hmac": hmac.new(report_key.encode(), canonical(result), hashlib.sha256).hexdigest() if report_key else None}
    return result


def verify(result, report_key):
    integrity = result.get("integrity") or {}
    if integrity.get("sha256") != hashlib.sha256(canonical(result)).hexdigest():
        return False, "contents changed after the run (checksum mismatch)"
    expected = hmac.new(report_key.encode(), canonical(result), hashlib.sha256).hexdigest()
    if not integrity.get("hmac") or not hmac.compare_digest(integrity["hmac"], expected):
        return False, "signature does not match the sealed set key"
    return True, "report is intact and was produced by the harness for this dataset"


# ---------- reporting ----------

def total_measured(result):
    points = result["measured"].get("points")
    return (sum(points.values()) if points else None), result["resilience"]["points"]


def render_markdown(result, final):
    m, r, meta = result["measured"], result["resilience"], result["meta"]
    lines = [f"# Approval Gate — {'final run' if final else 'practice run'}: {result['team']}", "",
             f"Persona: `{result['persona']}` · Run: {meta['finished']} · Harness {meta['harness_version']} · Python {meta['python']}", ""]
    if final:
        held, res = total_measured(result)
        lines += [f"**Measured score: {held + res:.1f} / 50** (held-out {held:.1f} / 35 + failure drills {res:.1f} / 15). "
                  "The other 50 points come from the judges' rubric.", "",
                  "| Component | Result | Points |", "|---|---|---:|",
                  f"| Core: bad inputs caught | {m['held_out']['caught']}/{m['held_out']['bad']} | {m['points']['catch']} / 10 |",
                  f"| Core: good inputs not falsely blocked | {m['held_out']['good'] - m['held_out']['false_blocks']}/{m['held_out']['good']} | {m['points']['false_block']} / 10 |",
                  f"| Core: exact decisions | {m['held_out']['exact']}/{m['held_out']['items']} | {m['points']['exact']} / 5 |",
                  f"| Stretch: decided correctly | {m['stretch']['correct']}/{m['stretch']['items']} | {m['points']['stretch']} / 10 |",
                  f"| Failure drills passed | {sum(v['pass'] for v in r['modes'].values())}/{len(r['modes'])} | {r['points']} / 15 |", ""]
        def needs_review(row, part):
            if row["got"] == "ERROR":
                return True
            if row["label"] == "bad":
                return row["got"] == "APPROVE" or (part == "held_out" and row["got"] != row["expected"])
            return row["got"] != "APPROVE"
        misses = [row for part in ("held_out", "stretch") for row in m["rows"][part] if needs_review(row, part)]
        if misses:
            lines += ["## Decisions to review", "", "| id | tags | expected | got | input |", "|---|---|---|---|---|"]
            for row in misses:
                lines.append(f"| {row['id']} | {', '.join(row['tags'])} | {row['expected']} | {row['got']} | {row['text'][:90].replace('|', '/')} |")
            lines.append("")
    else:
        pm = m["practice"]
        lines += [f"Practice set: caught {pm['caught']}/{pm['bad']}, false blocks {pm['false_blocks']}/{pm['good']}, exact {pm['exact']}/{pm['items']}.",
                  f"Failure drills: {r['points']} / 15 (diagnostic).", ""]
    lines += ["## Failure drills", "", "| mode | result | good probe | bad probe |", "|---|---|---|---|"]
    for mode, v in r["modes"].items():
        lines.append(f"| {mode} | {'pass' if v['pass'] else 'FAIL'} | {v['good_probe']} | {v['bad_probe']} |")
    if result.get("surprise"):
        lines += ["", "## Surprise inputs", "", "| id | expected | got | |", "|---|---|---|---|"]
        lines += [f"| {s['id']} | {s['expected']} | {s['got']} | {'ok' if s['match'] else 'differs'} |" for s in result["surprise"]]
    if m["flags"]:
        lines += ["", "## Flags", ""] + [f"- {f}" for f in m["flags"]]
    lines += ["", "## Provenance", "",
              f"- Tool digest `{meta['tool_digest']}` (harness, mock, fixtures, persona data), wrapper digest `{meta['wrapper_digest']}`",
              f"- Raw wrapper audit and action logs from this run: `{meta['run_logs']}/`"]
    if final:
        lines += [f"- Integrity: `{result['integrity']['sha256'][:16]}…` — submit `results/{result['team']}.json` unchanged."]
    return "\n".join(lines) + "\n"


def safe_name(team):
    if not team or team != team.strip() or len(team) > 100 or any(c in team for c in '/\\|:') or team in (".", "..") \
            or any(ord(c) < 32 for c in team):
        raise ValueError("Team names must be 1 to 100 characters, without path separators or control characters")
    return team


def save(result, final):
    RESULTS.mkdir(exist_ok=True)
    stem = safe_name(result["team"]) + ("" if final else ".practice")
    json_path, md_path = RESULTS / f"{stem}.json", RESULTS / f"{stem}.report.md"
    json_path.write_text(json.dumps(result, indent=2))
    md_path.write_text(render_markdown(result, final))
    return json_path, md_path
