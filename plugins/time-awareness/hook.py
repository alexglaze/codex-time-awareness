#!/usr/bin/env python3
"""Give local Codex chats a clock and persistent, per-chat elapsed time."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if os.name == "nt":
    import msvcrt
else:
    import fcntl


CONTEXT_EVENTS = {"SessionStart", "UserPromptSubmit"}
RECORD_EVENTS = {"UserPromptSubmit", "Stop", "Interrupt"}


def data_directory():
    """Plugin data must survive cache refreshes and never be written into the package."""
    plugin_data = os.environ.get("PLUGIN_DATA")
    if plugin_data:
        return Path(plugin_data)
    return Path(__file__).resolve().parent


def read_object(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@contextmanager
def file_lock(path, timeout=1.0):
    """Serialize updates on Windows and Unix, within the hook's three-second budget."""
    with path.open("a+b") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timestamp record is busy") from error
                time.sleep(0.025)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def atomic_write(path, value):
    """Replace a complete record; interruption cannot leave half-written JSON."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".time-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def parse_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


def elapsed(seconds):
    remaining = int(seconds)
    parts = []
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        amount, remaining = divmod(remaining, size)
        if amount:
            parts.append("{} {}{}".format(amount, unit, "" if amount == 1 else "s"))
        if len(parts) == 2:
            break
    return ", ".join(parts) or "less than 1 second"


def build_context(now, previous, zone_name, unavailable=False):
    zone = None
    zone_unavailable = False
    if zone_name:
        try:
            zone = ZoneInfo(zone_name)
        except ZoneInfoNotFoundError:
            zone_unavailable = True
    local = now.astimezone(zone)
    zone_label = zone_name if zone else local.tzname()
    lines = [
        "Time awareness (clock sampled for this event):",
        "Current local date/time: {} ({}, {}).".format(
            local.isoformat(timespec="seconds"), zone_label, local.strftime("%A")
        ),
        "Current UTC: {}.".format(now.isoformat(timespec="seconds")),
    ]
    if zone_unavailable:
        lines.append("Configured IANA timezone is unavailable; showing the computer's local timezone. "
                     "Check the timezone name or install tzdata for this Python interpreter.")
    previous_activity = parse_timestamp(previous.get("last_activity_at"))
    if previous_activity:
        gap = (now - previous_activity).total_seconds()
        labels = {
            "Stop": "the last recorded assistant turn ending",
            "Interrupt": "the last recorded turn interruption",
            "UserPromptSubmit": "the previous recorded message submission (not necessarily idle time)",
        }
        label = labels.get(previous.get("last_activity_event"), "the previous recorded chat event")
        previous_local = previous_activity.astimezone(zone)
        lines.append("Previous chat activity: {} ({}).".format(
            previous_local.isoformat(timespec="seconds"), label
        ))
        if gap >= 0:
            lines.append("Elapsed since that event: {} ({} seconds).".format(elapsed(gap), int(gap)))
        else:
            lines.append("The saved timestamp is in the future; elapsed time is unknown (clock changed).")
    else:
        lines.append("No earlier timestamp is available for this chat; the prior gap is unknown.")
    previous_prompt = parse_timestamp(previous.get("last_prompt_at"))
    if previous_prompt and previous_prompt != previous_activity and now >= previous_prompt:
        lines.append("Elapsed since the previous recorded message submission: {}.".format(
            elapsed((now - previous_prompt).total_seconds())
        ))
    if unavailable:
        lines.append("Timestamp storage is unavailable; only the current clock is reliable.")
    lines.append(
        "Use this clock for current relative dates; keep historical 'today'/'tomorrow' tied to "
        "their original messages. A gap does not establish what the user did or that work "
        "continued. Account for stale time-sensitive assumptions when relevant. Use this "
        "silently unless the timing matters to the request. This is a snapshot, not a live clock."
    )
    return "\n".join(lines)


def handle(payload, state_dir, zone_name=None, now=None):
    if not isinstance(payload, dict):
        return {}
    event = payload.get("hook_event_name")
    if event not in CONTEXT_EVENTS | RECORD_EVENTS:
        return {}
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    session = payload.get("session_id")
    previous = {}
    unavailable = False
    if isinstance(session, str) and session:
        # Hash the opaque id: it cannot escape the directory or expose chat titles.
        key = hashlib.sha256(session.encode("utf-8")).hexdigest()
        try:
            state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with file_lock(state_dir / (key + ".lock")):
                path = state_dir / (key + ".json")
                previous = read_object(path)
                turn = payload.get("turn_id")
                # A delayed completion from an older turn must not replace a newer prompt.
                stale_end = event in {"Stop", "Interrupt"} and (
                    not turn or turn != previous.get("turn_id")
                )
                if event in RECORD_EVENTS and not stale_end:
                    current = dict(previous)
                    current.update(last_activity_at=now.isoformat(), last_activity_event=event)
                    if event == "UserPromptSubmit":
                        current.update(last_prompt_at=now.isoformat(), turn_id=turn)
                    atomic_write(path, current)
        except OSError:
            unavailable = True
    if event not in CONTEXT_EVENTS:
        return {}
    return {"hookSpecificOutput": {
        "hookEventName": event,
        "additionalContext": build_context(now, previous, zone_name, unavailable),
    }}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=data_directory() / "state")
    parser.add_argument("--timezone", help="IANA timezone; defaults to settings.json or the host timezone")
    args = parser.parse_args()
    settings = read_object(data_directory() / "settings.json")
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        result = handle(payload, args.state_dir, args.timezone or settings.get("timezone"))
    except Exception as error:
        # An optional clock must never reject a prompt or restart an assistant turn.
        print("time-awareness: {}".format(type(error).__name__), file=sys.stderr)
        result = {}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
