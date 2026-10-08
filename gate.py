#!/usr/bin/env python3
"""Approval Gate: everything runs on your own machine. No network exposure, no judge laptop.

  python3 gate.py try      --persona customer_support "Your 30% loyalty discount applies to the Pro plan."
  python3 gate.py practice --persona customer_support
  python3 gate.py serve    --persona customer_support                         (demo mode: your persona page + mock)
  python3 gate.py results                                                     (color-coded latest results in your browser)

practice and judge open the results page themselves when they finish (use --no-show to skip it).
  python3 gate.py judge    --persona customer_support --team "Team Name"      (final set; passphrase released at 2:30 PM)
  python3 gate.py verify   results/<team>.json --key <passphrase>
  python3 gate.py digest

Each command starts a private mock and your wrapper on 127.0.0.1, points the wrapper at the mock through EZIO_URL, and
tears both down afterwards. Your wrapper is started with `python3 wrapper/serve.py`; for another language set
GATE_WRAPPER_CMD (it must read HOST, PORT and EZIO_URL and serve POST /decide and GET /health).
"""
import argparse
import getpass
import json
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import judging, results_server, runner, seal  # noqa: E402


def add_common(sp):
    sp.add_argument("--persona", default=os.getenv("GATE_PERSONA"), choices=judging.PERSONAS,
                    help="customer_support, paralegal or financial_analyst (or set GATE_PERSONA)")
    sp.add_argument("--wrapper-cmd", help="command that starts your wrapper (default: python3 wrapper/serve.py)")


def need_persona(args, parser):
    if not args.persona:
        parser.error("--persona is required (or set GATE_PERSONA)")


def cmd_try(args):
    """One ordinary decision. Uses your wrapper's normal audit_log.jsonl and actions.log: the demo audit sample."""
    item = {"text": args.text, "context": json.loads(args.context or "{}")}
    with runner.Stack(latency=args.latency, wrapper_cmd=args.wrapper_cmd) as stack:
        result = judging.call_decide(stack.wrapper_url, item, args.persona, args.timeout)
        if not result["ok"]:
            print(f"ERROR: {result['error']}\n{stack.wrapper_stderr_tail()}")
            return 1
    print(f"decision: {result['decision']}  ({result['seconds']}s)\nreason:   {result['reason']}")
    print(f"action:   {'EXECUTED' if result['action_executed'] else 'not executed' if result['action_executed'] is False else 'unknown (custom wrapper)'}")
    print("evidence:")
    for line in result["evidence"] or ["(none)"]:
        print(f"  - {line}")
    return 0


def cmd_serve(args):
    """Demo mode: private mock plus your wrapper (and its UI) until Ctrl-C. Normal audit and action logs."""
    import time
    with runner.Stack(latency=args.latency, wrapper_cmd=args.wrapper_cmd) as stack:
        print(f"Wrapper and UI: {stack.wrapper_url}/   (mock behind it at {stack.mock_url})\nCtrl-C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0


def cmd_results(args):
    """Open the newest file in results/ (practice or final) as a color-coded page on localhost."""
    results_server.serve(args.port, not args.no_open)
    return 0


def maybe_show(args):
    """Open the results page when a run ends. Default: yes in an interactive terminal, no in scripts and CI."""
    show = args.show if args.show is not None else sys.stdout.isatty() and not os.getenv("GATE_NO_SHOW")
    if show:
        results_server.serve(args.port, True)


def cmd_practice(args):
    result = judging.evaluate(args.persona, args.team, judging.load_practice(args.persona), latency=args.latency,
                              workers=args.workers, timeout=args.timeout, wrapper_cmd=args.wrapper_cmd)
    pm = result["measured"]["practice"]
    print(f"\nPRACTICE SET: caught {pm['caught']}/{pm['bad']}, false blocks {pm['false_blocks']}/{pm['good']}, exact {pm['exact']}/{pm['items']}")
    print(f"FAILURE DRILLS: {result['resilience']['points']}/15")
    for f in result["measured"]["flags"]:
        print(f"  FLAG: {f}")
    json_path, md_path = judging.save(result, final=False)
    print(f"\nSaved {md_path.relative_to(runner.REPO)}")
    maybe_show(args)
    return 0


def cmd_judge(args):
    key = args.key or os.getenv("GATE_FINAL_KEY") or getpass.getpass("Final-set passphrase (released by organizers): ")
    try:
        judging.safe_name(args.team)
        final = judging.load_final(key)
    except (seal.SealError, ValueError) as e:
        print(f"Cannot start: {e}")
        return 2
    spec = final["personas"].get(args.persona)
    if not spec:
        print(f"The sealed set has no data for {args.persona}")
        return 2
    result = judging.evaluate(args.persona, args.team, judging.load_practice(args.persona), spec["held_out"], spec["stretch"],
                              spec.get("surprise_pool"), final["extra_known_claims"], latency=args.latency,
                              workers=args.workers, timeout=args.timeout, wrapper_cmd=args.wrapper_cmd, report_key=final["report_key"])
    result["meta"]["dataset_id"] = final["dataset_id"]
    judging.sign(result, final["report_key"])
    held, res = judging.total_measured(result)
    m = result["measured"]
    print(f"\n  core: caught {m['held_out']['caught']}/{m['held_out']['bad']}, false blocks {m['held_out']['false_blocks']}/{m['held_out']['good']}, "
          f"exact {m['held_out']['exact']}/{m['held_out']['items']}; stretch correct {m['stretch']['correct']}/{m['stretch']['items']}")
    print(f"  measured points   {held:.1f}/35 held-out + {res:.1f}/15 failure drills = {held + res:.1f}/50")
    for f in m["flags"]:
        print(f"  FLAG: {f}")
    json_path, md_path = judging.save(result, final=True)
    print(f"\nSaved {json_path.relative_to(runner.REPO)} and {md_path.relative_to(runner.REPO)}. Submit both files unchanged.")
    maybe_show(args)
    return 0


def cmd_verify(args):
    result = json.loads(Path(args.report).read_text())
    key = args.key or os.getenv("GATE_FINAL_KEY") or getpass.getpass("Final-set passphrase: ")
    try:
        final = judging.load_final(key)
    except seal.SealError as e:
        print(e)
        return 2
    ok, message = judging.verify(result, final["report_key"])
    print(("VALID: " if ok else "INVALID: ") + message)
    if ok and result["meta"].get("dataset_id") != final["dataset_id"]:
        print("WARNING: report was produced against a different sealed set")
        return 1
    return 0 if ok else 1


def cmd_digest(args):
    print(f"tool digest    {judging.tool_digest()}\nwrapper digest {judging.wrapper_digest()}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("try", help="run one input through your wrapper and its mock action")
    add_common(t)
    t.add_argument("text")
    t.add_argument("--context")
    t.add_argument("--latency", default="0", help="mock latency in seconds, e.g. 0 or 2-6 (default 0)")
    t.add_argument("--timeout", type=float, default=60)
    t.set_defaults(fn=cmd_try)

    sv = sub.add_parser("serve", help="run your wrapper, UI and a mock for the demo")
    add_common(sv)
    sv.add_argument("--latency", default="2-6", help="mock latency, e.g. 0 or 2-6 (default 2-6, like the real challenge)")
    sv.set_defaults(fn=cmd_serve)

    pr = sub.add_parser("practice", help="practice set plus all six failure drills")
    add_common(pr)
    pr.add_argument("--team", default="local")
    pr.add_argument("--latency", default="0")
    pr.add_argument("--workers", type=int, default=4)
    pr.add_argument("--timeout", type=float, default=60)
    pr.add_argument("--show", dest="show", action="store_true", default=None, help="open the results page when the run finishes (default in a terminal)")
    pr.add_argument("--no-show", dest="show", action="store_false", help="do not open the results page")
    pr.add_argument("--port", type=int, default=results_server.DEFAULT_PORT)
    pr.set_defaults(fn=cmd_practice)

    j = sub.add_parser("judge", help="the final judging set (passphrase released in the last 30 minutes)")
    add_common(j)
    j.add_argument("--team", required=True)
    j.add_argument("--key", help="passphrase (or set GATE_FINAL_KEY, or you will be asked)")
    j.add_argument("--latency", default="1-2", help="keep the default for the official run")
    j.add_argument("--workers", type=int, default=4)
    j.add_argument("--timeout", type=float, default=60, help="keep the default for the official run")
    j.add_argument("--show", dest="show", action="store_true", default=None, help="open the results page when the run finishes (default in a terminal)")
    j.add_argument("--no-show", dest="show", action="store_false", help="do not open the results page")
    j.add_argument("--port", type=int, default=results_server.DEFAULT_PORT)
    j.set_defaults(fn=cmd_judge)

    rs = sub.add_parser("results", help="show the latest practice or final results in your browser")
    rs.add_argument("--port", type=int, default=results_server.DEFAULT_PORT)
    rs.add_argument("--no-open", action="store_true", help="print the address without opening a browser")
    rs.set_defaults(fn=cmd_results)

    v = sub.add_parser("verify", help="check that a results file is intact (organizers)")
    v.add_argument("report")
    v.add_argument("--key")
    v.set_defaults(fn=cmd_verify)

    d = sub.add_parser("digest", help="print the tool and wrapper digests")
    d.set_defaults(fn=cmd_digest)

    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # still tear down the mock and wrapper
    if args.cmd in ("try", "practice", "judge", "serve"):
        need_persona(args, parser)
    if getattr(args, "workers", 1) <= 0 or getattr(args, "timeout", 1) <= 0:
        parser.error("workers and timeout must be positive")
    try:
        sys.exit(args.fn(args))
    except runner.StackError as e:
        sys.exit(f"Could not start the wrapper stack:\n{e}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
