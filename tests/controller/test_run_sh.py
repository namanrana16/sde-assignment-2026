"""A10: run.sh smoke tests with an unreachable / fake sim (Git Bash or any POSIX bash)."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def find_bash() -> str | None:
    for cand in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")):
        if cand and Path(cand).exists() and "system32" not in cand.lower():
            return cand
    return None


BASH = find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no POSIX bash")


def run_sh(url, **env):
    full = {**os.environ, "PYTHON": sys.executable, **env}
    return subprocess.run([BASH, "run.sh", url], cwd=ROOT, env=full, capture_output=True, text=True, timeout=120)


def test_unreachable_sim_exits_3_after_the_wait_timeout():
    r = run_sh("http://127.0.0.1:9", WAIT_TIMEOUT_S="3")
    assert r.returncode == 3, r.stderr
    assert "not up" in r.stderr


def test_run_sh_is_valid_bash_with_lf_endings():
    assert subprocess.run([BASH, "-n", "run.sh"], cwd=ROOT).returncode == 0
    assert b"\r" not in (ROOT / "run.sh").read_bytes()


def test_deterministic_crash_reaches_the_restart_cap(tmp_path):
    """A fake sim whose /sim/state never moves while the controller dies: cap, exit 1."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"sim_minute": 5, "finished": false}' if self.path == "/sim/state" else b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = run_sh(f"http://127.0.0.1:{srv.server_address[1]}", MAX_RESTARTS="1", RESTART_WAIT_S="2",
                   STATE_DIR=str(tmp_path / "st"), WAIT_TIMEOUT_S="10")
    finally:
        srv.shutdown()
    assert r.returncode == 1, r.stderr
    assert "restart cap reached" in r.stderr


def test_every_pip_call_in_run_sh_has_a_timeout_and_no_version_check():
    pip_lines = [l for l in (ROOT / "run.sh").read_text().splitlines() if "pip install" in l and "echo" not in l]
    assert len(pip_lines) >= 3
    for line in pip_lines:
        assert "--timeout 15" in line and "--retries 1" in line and "--disable-pip-version-check" in line, line
