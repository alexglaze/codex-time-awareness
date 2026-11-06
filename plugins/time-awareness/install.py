#!/usr/bin/env python3
"""Install or remove the local Codex time-awareness hook registrations."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
from zoneinfo import ZoneInfo

from hook import atomic_write


EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "Interrupt")


def is_ours(handler, target):
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    try:
        arguments = shlex.split(handler.get("command", ""))
        return len(arguments) > 1 and arguments[1] == str(target / "hook.py")
    except ValueError:
        return False


def configure(config, target, remove=False):
    config = json.loads(json.dumps(config))
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Existing hooks configuration is not an object; leaving it untouched.")
    command = shlex.join([sys.executable, str(target / "hook.py")])
    for event in EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError("Invalid existing hook groups for " + event)
        retained = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("Invalid existing hook group for " + event)
            handlers = group["hooks"]
            filtered = [handler for handler in handlers if not is_ours(handler, target)]
            if filtered or not handlers:
                retained.append(dict(group, hooks=filtered))
        if not remove:
            handler = {"type": "command", "command": command, "timeout": 3}
            if event in ("SessionStart", "UserPromptSubmit"):
                handler["additionalContextLimit"] = 600
            retained.append({"hooks": [handler]})
        if retained:
            hooks[event] = retained
        else:
            hooks.pop(event, None)
    return config


def install(codex_dir, zone_name=None, remove=False, dry_run=False):
    codex_dir = codex_dir.expanduser().resolve()
    target = codex_dir / "time-awareness"
    hooks_path = codex_dir / "hooks.json"
    if zone_name:
        ZoneInfo(zone_name)  # Validate before changing anything.
    original = hooks_path.read_bytes() if hooks_path.exists() else None
    config = json.loads(original) if original is not None else {}
    if not isinstance(config, dict):
        raise ValueError("Existing hooks.json is not an object; leaving it untouched.")
    updated = configure(config, target, remove)
    summary = {
        "action": "remove" if remove else "install",
        "hooks_file": str(hooks_path),
        "script": str(target / "hook.py"),
        "events": list(EVENTS),
        "dry_run": dry_run,
        "trust": "Review in Codex Settings > Hooks or CLI /hooks; trust is not changed by this installer.",
    }
    if dry_run:
        return summary
    codex_dir.mkdir(parents=True, exist_ok=True)
    if not remove:
        target.mkdir(exist_ok=True)
        source = Path(__file__).with_name("hook.py")
        if source.resolve() != (target / "hook.py").resolve():
            shutil.copy2(source, target / "hook.py")
        if zone_name:
            atomic_write(target / "settings.json", {"timezone": zone_name})
    if updated != config:
        if original is not None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = hooks_path.with_name("hooks.json.time-awareness-" + stamp + ".bak")
            backup.write_bytes(original)
            summary["backup"] = str(backup)
        # Refuse to replace a config edited while this installer was running.
        actual = hooks_path.read_bytes() if hooks_path.exists() else None
        if actual != original:
            raise RuntimeError("hooks.json changed during installation; rerun the installer.")
        atomic_write(hooks_path, updated)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path,
                        default=Path(os.environ.get("CODEX_HOME") or "~/.codex"))
    parser.add_argument("--timezone", help="IANA timezone, e.g. America/New_York; otherwise use the host")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--remove", action="store_true", help="Remove registrations; keep timestamp data")
    args = parser.parse_args()
    print(json.dumps(install(args.codex_home, args.timezone, args.remove, args.dry_run), indent=2))


if __name__ == "__main__":
    main()
