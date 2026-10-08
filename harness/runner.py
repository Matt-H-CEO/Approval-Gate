"""Starts a private mock and the team's wrapper on loopback, and tears both down. Nothing is exposed to the network.

The harness owns the mock, so a wrapper can never be pointed at the wrong one, and the failure-mode control key
exists only inside the mock process: it is never in the wrapper's environment.
"""
import json
import os
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOCK_KEY = "mock-team-key"
DEFAULT_WRAPPER = [sys.executable, "wrapper/serve.py"]


class StackError(Exception):
    pass


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wrapper_command(override=None):
    """GATE_WRAPPER_CMD lets a team run a wrapper written in any language (it must honour HOST, PORT, EZIO_URL)."""
    raw = override or os.getenv("GATE_WRAPPER_CMD")
    return shlex.split(raw, posix=os.name != "nt") if raw else list(DEFAULT_WRAPPER)


def wait_for(url, process, label, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise StackError(f"The {label} exited during startup (exit code {process.returncode}).")
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise StackError(f"The {label} did not answer {url} within {seconds:.0f} seconds.")


class Stack:
    """Context manager: mock on a free loopback port, then the wrapper pointed at it.

    log_dir: where the wrapper writes audit_log.jsonl / actions.log (via GATE_LOG_DIR). None keeps the wrapper's
    normal log location, which is what `gate.py try` wants for the demo audit sample.
    """

    def __init__(self, extra_claims=None, latency="1-2", log_dir=None, wrapper_cmd=None, startup_seconds=45):
        self.extra_claims, self.latency, self.log_dir = extra_claims, latency, log_dir
        self.command, self.startup_seconds = wrapper_command(wrapper_cmd), startup_seconds
        self.admin_key = secrets.token_urlsafe(32)
        self.processes, self.tmp = [], None
        self.mock_url = self.wrapper_url = None

    def __enter__(self):
        try:
            self._start()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _spawn(self, args, env, cwd, stderr_path):
        kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        process = subprocess.Popen(args, env=env, cwd=cwd, stdout=subprocess.DEVNULL,
                                   stderr=open(stderr_path, "wb"), **kwargs)
        self.processes.append(process)
        return process

    def _start(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="approval-gate-")
        tmp = Path(self.tmp.name)
        mock_env = {**os.environ, "HOST": "127.0.0.1", "PORT": str(free_port()), "ADMIN": "1", "MOCK_ADMIN_KEY": self.admin_key,
                    "MOCK_API_KEY": MOCK_KEY, "LATENCY": self.latency, "CHAOS": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        mock_env.pop("EXTRA_KNOWN_CLAIMS", None)
        if self.extra_claims:
            claims_file = tmp / "extra_known_claims.json"
            claims_file.write_text(json.dumps(self.extra_claims))
            claims_file.chmod(0o600)
            mock_env["EXTRA_KNOWN_CLAIMS"] = str(claims_file)
        self.mock_url = f"http://127.0.0.1:{mock_env['PORT']}"
        mock = self._spawn([sys.executable, str(REPO / "mock_ezio.py")], mock_env, REPO, tmp / "mock.stderr")
        try:
            wait_for(self.mock_url + "/v1/health", mock, "mock", 20)
        except StackError as e:
            raise StackError(f"{e}\n{(tmp / 'mock.stderr').read_text(errors='replace')[-800:]}") from None

        env = {k: v for k, v in os.environ.items() if k not in ("MOCK_ADMIN_KEY", "EXTRA_KNOWN_CLAIMS")}
        port = free_port()
        while f"http://127.0.0.1:{port}" == self.mock_url:
            port = free_port()
        env.update({"HOST": "127.0.0.1", "PORT": str(port), "EZIO_URL": self.mock_url, "EZIO_KEY": MOCK_KEY,
                    "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        if self.log_dir:
            Path(self.log_dir).mkdir(parents=True, exist_ok=True)
            env["GATE_LOG_DIR"] = str(self.log_dir)
        else:
            env.pop("GATE_LOG_DIR", None)
        self.wrapper_url = f"http://127.0.0.1:{port}"
        self.wrapper_stderr = tmp / "wrapper.stderr"
        try:
            wrapper = self._spawn(self.command, env, REPO, self.wrapper_stderr)
        except OSError as e:
            raise StackError(f"Could not start the wrapper with {self.command}: {e}") from None
        try:
            wait_for(self.wrapper_url + "/health", wrapper, "wrapper", self.startup_seconds)
        except StackError as e:
            raise StackError(f"{e}\nCommand: {' '.join(self.command)}\nLast stderr:\n"
                             f"{self.wrapper_stderr.read_text(errors='replace')[-1200:]}") from None

    def set_mode(self, mode):
        request = urllib.request.Request(self.mock_url + "/admin/mode", data=json.dumps({"mode": mode}).encode(),
                                         headers={"Authorization": f"Bearer {self.admin_key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()

    def wrapper_stderr_tail(self, chars=1200):
        try:
            return self.wrapper_stderr.read_text(errors="replace")[-chars:]
        except (AttributeError, OSError):
            return ""

    def __exit__(self, *exc):
        for process in reversed(self.processes):
            if process.poll() is None:
                try:
                    if os.name == "nt":
                        process.terminate()
                    else:
                        os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except OSError:
                        pass
        self.processes = []
        if self.tmp:
            self.tmp.cleanup()
            self.tmp = None
