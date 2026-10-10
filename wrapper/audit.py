"""Audit log: one JSON line per decision. Done for you; extend the fields if your policy needs more."""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

# gate.py judge/try set GATE_LOG_DIR so evaluation runs never pollute your demo audit sample.
LOG = Path(os.getenv("GATE_LOG_DIR") or Path(__file__).parent) / "audit_log.jsonl"
_lock = threading.Lock()


def record(persona, text, context, decision):
    entry = {
        "decision_id": getattr(decision, "decision_id", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "persona": persona,
        "input": text,
        "context": context,
        "decision": decision.decision,
        "reason": decision.reason,
        "evidence": decision.evidence,
        "findings": getattr(decision, "findings", []),
        "coverage": getattr(decision, "coverage", {}),
        "welfare": getattr(decision, "welfare", None),
        "ezio_status": getattr(decision, "ezio_status", ""),
        "preferences": getattr(decision, "preferences", None),
        "issues": getattr(decision, "issues", []),
        "ezio_flags": getattr(decision, "ezio_flags", []),
        "recommendation": getattr(decision, "recommendation", None),
        "baseline": getattr(decision, "baseline", None),
        "ezio": decision.ezio,
    }
    with _lock, LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")
