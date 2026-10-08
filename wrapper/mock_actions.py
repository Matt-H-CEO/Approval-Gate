"""Mock actions. They log instead of executing, so a blocked action is visible by its absence."""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

LOG = Path(os.getenv("GATE_LOG_DIR") or Path(__file__).parent) / "actions.log"
_lock = threading.Lock()


def _record(name, **args):
    line = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": name, "args": args}
    print(f"[MOCK ACTION] {name} executed")
    with _lock, LOG.open("a") as f:
        f.write(json.dumps(line) + "\n")


def send_email(to, subject, body):
    _record("send_email", to=to, subject=subject, body=body)


def submit_memo_to_partner(memo, citations=None):
    _record("submit_memo_to_partner", memo=memo, citations=citations or [])


def publish_note_to_client(note):
    _record("publish_note_to_client", note=note)


def run_for_persona(persona, text):
    if persona == "customer_support":
        send_email("customer@example.com", "Re: your question", text)
    elif persona == "paralegal":
        submit_memo_to_partner(text)
    elif persona == "financial_analyst":
        publish_note_to_client(text)
    else:
        raise ValueError(f"unknown persona: {persona}")
