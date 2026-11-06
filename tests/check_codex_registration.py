"""Check real Codex hook discovery and execute its selected commands, without model calls."""

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
EVENTS = {"sessionStart": "SessionStart", "userPromptSubmit": "UserPromptSubmit",
          "stop": "Stop", "interrupt": "Interrupt"}


def main():
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("Install the Codex CLI before running this integration check")
    with tempfile.TemporaryDirectory(prefix="time-awareness-codex-") as temporary:
        root = Path(temporary)
        config_home = root / "codex"
        config_home.mkdir()
        env = dict(os.environ, CODEX_HOME=str(config_home))
        for args in (["--version"], ["plugin", "marketplace", "add", str(ROOT), "--json"],
                     ["plugin", "add", "time-awareness@time-awareness-community", "--json"]):
            result = subprocess.run([codex] + args, env=env, shell=os.name == "nt",
                                    capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
            print(result.stdout.strip())
        with (root / "server.log").open("w", encoding="utf-8") as log:
            server = subprocess.Popen([codex, "app-server", "--stdio"], env=env,
                shell=os.name == "nt", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=log, text=True, encoding="utf-8")
            messages = queue.Queue()

            def read_output():
                for line in server.stdout:
                    messages.put(json.loads(line))
                messages.put(None)

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()

            def send(value):
                server.stdin.write(json.dumps(value) + "\n")
                server.stdin.flush()

            def request(number, method, params):
                send({"jsonrpc": "2.0", "id": number, "method": method, "params": params})
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    message = messages.get(timeout=max(0.1, deadline - time.monotonic()))
                    if message is None:
                        raise RuntimeError("Codex exited before returning " + method)
                    if message.get("id") == number:
                        if "error" in message:
                            raise RuntimeError(str(message["error"]))
                        return message["result"]
                raise TimeoutError(method)

            try:
                request(1, "initialize", {"clientInfo": {"name": "time_awareness_check", "version": "0.2.0"}})
                send({"jsonrpc": "2.0", "method": "initialized"})
                result = request(2, "hooks/list", {"cwds": [str(ROOT)]})
                errors = [error for entry in result["data"] for error in entry["errors"]]
                assert not errors, errors
                hooks = [hook for entry in result["data"] for hook in entry["hooks"]
                         if hook.get("pluginId") == "time-awareness@time-awareness-community"]
                assert len(hooks) == 4 and {h["eventName"] for h in hooks} == set(EVENTS), hooks
                for hook in hooks:
                    assert hook["enabled"], hook
                    if os.name == "nt":
                        assert hook["command"].startswith('python "%PLUGIN_ROOT%'), hook
                    plugin_root = Path(hook["sourcePath"]).parent.parent
                    hook_env = dict(env, PLUGIN_ROOT=str(plugin_root), PLUGIN_DATA=str(root / "hook-data"))
                    event = EVENTS[hook["eventName"]]
                    response = subprocess.run(hook["command"], shell=True, env=hook_env, cwd=root,
                        input=json.dumps({"hook_event_name": event, "session_id": "registration-test", "turn_id": "1"}),
                        capture_output=True, text=True, encoding="utf-8", timeout=3, check=True)
                    output = json.loads(response.stdout)
                    if event in ("SessionStart", "UserPromptSubmit"):
                        assert output["hookSpecificOutput"]["hookEventName"] == event, output
                        assert "Current local date/time:" in output["hookSpecificOutput"]["additionalContext"]
                    else:
                        assert output == {}, output
                print("Codex discovered all four hooks; all selected commands returned valid output.")
                print("Hook trust was not changed; no model requests were made.")
            finally:
                # Closing stdin lets the real server exit, including when launched via codex.cmd.
                server.stdin.close()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(server.pid), "/T", "/F"],
                                       capture_output=True, timeout=10)
                    else:
                        server.kill()
                    server.wait(timeout=10)
                reader.join(timeout=5)
                server.stdout.close()


if __name__ == "__main__":
    main()
