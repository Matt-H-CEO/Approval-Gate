import json
import mimetypes
import os
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mock_actions
import scenario
from datetime import datetime, timezone
from wrapper import APPROVE, decide

UI_DIR = Path(__file__).parent / "ui"
PERSONAS_DIR = Path(__file__).resolve().parent.parent / "personas"

PERSONAS = {"customer_support", "paralegal", "financial_analyst"}
LOG_DIR = Path(os.getenv("GATE_LOG_DIR") or Path(__file__).parent)
FAST_SECONDS = 15  # a release this soon after the gate held the draft is marked as a possible rubber stamp
GENERIC = {"ok", "okay", "fine", "approved", "approve", "lgtm", "looks good", "looks fine", "good", "yes", "send", "send it",
           "release", "checked", "all good", "no issues", "go ahead", "sure"}


def tail_jsonl(path, limit=40):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows[::-1]


def samples(persona):
    """Practice inputs (text and context only) so a UI can offer 'simulate from a test input'."""
    path = PERSONAS_DIR / persona / "practice_set.json"
    items = json.loads(path.read_text())["items"]
    return [{"id": i["id"], "text": i["text"], "context": i["context"]} for i in items]


def ui_file(path):
    """Static files from wrapper/ui/, so a team can use plain HTML, a built SPA, or anything a browser runs."""
    name = "persona.html" if path in ("/", "") else path.lstrip("/")
    target = (UI_DIR / name).resolve()
    if UI_DIR.resolve() not in target.parents or not target.is_file():
        return None
    return target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, code, body):
        raw = json.dumps(body, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        route, _, query = self.path.partition("?")
        if route == "/health":
            return self.send_json(200, {"status": "ok"})
        if route == "/api/audit":
            rows = [{k: r.get(k) for k in ("decision_id", "timestamp", "persona", "input", "context", "decision", "reason", "ezio_status",
                                           "preferences", "issues", "ezio_flags", "recommendation", "baseline", "findings")}
                    for r in tail_jsonl(LOG_DIR / "audit_log.jsonl")]
            return self.send_json(200, {"audit": rows, "reviews": tail_jsonl(LOG_DIR / "review_log.jsonl"),
                                        "actions": tail_jsonl(LOG_DIR / "actions.log"),
                                        "dispatch_actions": tail_jsonl(LOG_DIR / scenario.DISPATCH_LOG)})
        if route == "/api/scenarios":
            return self.send_json(200, {"scenarios": scenario.list_scenarios()})
        if route == "/api/samples":
            persona = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("persona")
            if persona not in PERSONAS:
                return self.send_json(400, {"error": "Unknown persona"})
            return self.send_json(200, {"samples": samples(persona)})
        served = ui_file(route)
        if served:
            raw, content_type = served
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_json(404, {"error": "Unknown endpoint"})

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 1_000_000:
            raise ValueError("Body must be 1 to 1000000 bytes")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("Body must be a JSON object")
        return payload

    def do_POST(self):
        if self.path == "/api/review":
            return self.review()
        if self.path == "/api/dispatch":
            return self.run_dispatch()
        if self.path == "/api/scenario":
            return self.run_scenario()
        if self.path == "/api/dispatch_gate":  # earlier variant; the UI uses /api/dispatch above
            return self.dispatch()
        if self.path != "/decide":
            return self.send_json(404, {"error": "Unknown endpoint"})
        self.connection.settimeout(10)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 1_000_000:
                raise ValueError("Body must be 1 to 1000000 bytes")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Body must be a JSON object")
            text, persona, context = payload.get("text"), payload.get("persona"), payload.get("context", {})
            if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                raise ValueError("text must be a non-empty string of at most 20000 characters")
            if not isinstance(persona, str) or persona not in PERSONAS:
                raise ValueError("Unknown persona")
            if not isinstance(context, dict):
                raise ValueError("context must be an object")
        except (ValueError, UnicodeDecodeError, OSError):
            return self.send_json(400, {"error": "Invalid request: supply text, persona, and an optional context object"})
        try:
            result = decide(text, context, persona)
            execute = result.decision == APPROVE
            if execute:
                mock_actions.run_for_persona(persona, text)
            self.send_json(200, {**asdict(result), "action_executed": execute})
        except Exception:
            self.send_json(500, {"error": "Wrapper failed; inspect the decision implementation"})


    def review(self):
        """A person releases or rejects a HELD draft. Anti-rubber-stamp rules are enforced here, not only in the page:
        the decision must exist in the audit log and be a HOLD; every flagged problem must be acknowledged; the reason
        must be specific; the time between the gate's decision and the release is recorded, and very fast releases are
        marked. Logged to review_log.jsonl, never actions.log (which must equal the gate's APPROVE decisions)."""
        try:
            p = self.read_json()
            verdict, reason = p.get("verdict"), " ".join(str(p.get("reason", "")).split())
            did = str(p.get("decision_id", ""))
            if verdict not in ("release", "reject"):
                raise ValueError("verdict must be release or reject")
            row = next((r for r in tail_jsonl(LOG_DIR / "audit_log.jsonl", 5000) if r.get("decision_id") == did), None)
            if row is None:
                raise ValueError("unknown decision_id: only decisions the gate logged can be reviewed")
            if row.get("decision") != "HOLD_FOR_REVIEW":
                raise ValueError("only HOLD_FOR_REVIEW decisions can be released; a BLOCK needs an edited draft")
            flags = row.get("findings") or []
            acked = p.get("acknowledged") or []
            if verdict == "release" and sorted(set(int(i) for i in acked)) != list(range(len(flags))):
                raise ValueError(f"acknowledge each of the {len(flags)} flagged problem(s) before releasing")
            if len(reason) < 15 or reason.lower().strip(".! ") in GENERIC:
                raise ValueError("give a specific reason (what you checked and against what); 'ok' or 'looks good' is not a review")
        except (ValueError, TypeError, UnicodeDecodeError, OSError) as e:
            return self.send_json(400, {"error": str(e)})
        now = datetime.now(timezone.utc)
        try:
            dwell = round((now - datetime.fromisoformat(row["timestamp"])).total_seconds())
        except (KeyError, ValueError):
            dwell = None
        entry = {"timestamp": now.isoformat(timespec="seconds"), "decision_id": did, "verdict": verdict, "reason": reason[:500],
                 "reviewer": str(p.get("reviewer") or "support lead")[:80], "input": row.get("input", "")[:2000],
                 "gate_reason": row.get("reason", "")[:500], "flags_acknowledged": len(flags) if verdict == "release" else 0,
                 "seconds_since_gate_decision": dwell, "fast_release": verdict == "release" and dwell is not None and dwell < FAST_SECONDS,
                 "outcome": "sent (mock), human-released" if verdict == "release" else "not sent, rejected by reviewer"}
        with (LOG_DIR / "review_log.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")
        return self.send_json(200, entry)

    def dispatch(self):
        """Aviation dispatch (MOCK): gate an AI recommendation (option + rationale) under stated stakeholder preferences."""
        try:
            p = self.read_json()
            option, rationale = str(p.get("option", "")), p.get("rationale")
            if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 20000:
                raise ValueError("rationale must be a non-empty string")
            ctx = p.get("context") if isinstance(p.get("context"), dict) else {}
            r, executed = scenario.decide_dispatch(option, rationale, p.get("preferences"), ctx)
        except (ValueError, UnicodeDecodeError, OSError) as e:
            return self.send_json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self.send_json(500, {"error": f"Dispatch gate failed: {type(e).__name__}"})
        return self.send_json(200, {**asdict(r), "action_executed": executed, "action": "sign_dispatch_release"})

    def run_dispatch(self):
        """Check one proposed dispatch release: an option plus the rationale a person would sign."""
        try:
            p = self.read_json()
            option = str(p.get("option") or "")
            rationale = str(p.get("rationale") or "")
            if not option or not rationale.strip():
                return self.send_json(400, {"error": "Supply an option and a rationale"})
            prefs = p.get("preferences") if isinstance(p.get("preferences"), dict) else None
            ctx = p.get("context") if isinstance(p.get("context"), dict) else {}
            result = scenario.dispatch("aviation_dispatch", option, rationale, prefs, ctx)
        except KeyError:
            return self.send_json(404, {"error": "Unknown option"})
        except (ValueError, UnicodeDecodeError, OSError) as e:
            return self.send_json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self.send_json(500, {"error": f"Dispatch failed: {type(e).__name__}"})
        row = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "decision_id": result["decision_id"], "persona": "aviation_dispatch",
               "input": result["rationale"], "context": dict(result.get("context") or {}, option=result["option"]),
               "decision": result["decision"], "reason": result["reason"], "ezio_status": "ok",
               "findings": [], "issues": result["issues"], "ezio_flags": result["ezio_flags"],
               "baseline": result["baseline"], "preferences": result["recommendation"]["preferences"],
               "recommendation": {k: result["recommendation"][k] for k in ("choice", "choice_label", "why", "tradeoff", "suggested_release")}}
        try:
            with (LOG_DIR / "audit_log.jsonl").open("a") as f:
                f.write(json.dumps(row) + "\n")
            if result["action_executed"]:
                with (LOG_DIR / "dispatch_actions.log").open("a") as f:
                    f.write(json.dumps({"timestamp": row["timestamp"], "decision_id": result["decision_id"],
                                        "option": result["option"], "outcome": "release signed (mock)"}) + "\n")
        except OSError:
            pass
        return self.send_json(200, result)

    def run_scenario(self):
        try:
            p = self.read_json()
            result = scenario.run(str(p.get("id")), p.get("preferences"))
        except KeyError:
            return self.send_json(404, {"error": "Unknown scenario"})
        except (ValueError, UnicodeDecodeError, OSError) as e:
            return self.send_json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self.send_json(500, {"error": f"Scenario failed: {type(e).__name__}"})
        return self.send_json(200, result)


def main():
    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "9000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Approval Gate wrapper on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
