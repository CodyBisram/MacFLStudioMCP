"""
File-based bridge for piano roll operations.

Works without loopMIDI / any MIDI device — the MCP server writes a request
file that the `ComposeWithLLM.pyscript` (piano roll pyscript) picks up when
it runs. The server triggers the pyscript by synthesising Ctrl+Alt+Y into the
FL Studio window. The pyscript then writes a state file we read back.

This complements `bridge_client.py` (the main file-bus channel for non-piano-roll
operations). Piano roll has its own file-bus because `flpianoroll` is sandboxed
to piano-roll scripts and can only be reached via the Cmd+Opt+Y / Ctrl+Alt+Y
keystroke trick.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("fl_studio_mcp.file_bridge")


def _piano_roll_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.path.expandvars("%USERPROFILE%")) / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    else:
        base = Path.home() / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    return base / "Piano roll scripts"


PR_DIR = _piano_roll_dir()
REQUEST_FILE = PR_DIR / "fLMCP_request.json"
STATE_FILE = PR_DIR / "fLMCP_state.json"
RESPONSE_FILE = PR_DIR / "fLMCP_response.json"


def is_installed() -> bool:
    """True if the ComposeWithLLM pyscript is present on disk."""
    return (PR_DIR / "ComposeWithLLM.pyscript").exists()


def _read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_request(action: dict) -> None:
    existing = _read_json(REQUEST_FILE)
    if not isinstance(existing, list):
        existing = []
    existing.append(action)
    _write_json(REQUEST_FILE, existing)


def clear_request_queue() -> None:
    _write_json(REQUEST_FILE, [])


def clear_state() -> None:
    """Reset the state file to a JSON null sentinel so wait_for_state keeps
    polling without needing to delete+recreate the file.

    We write 'null' rather than unlinking because:
      - _read_json returns Python None for JSON null → wait_for_state loops
      - The pyscript only needs to *write* to the file, not *create* it
      - On Mac, FL's pyscript sandbox may not be able to create new files
        (audit-hook restriction); keeping the file alive avoids that risk
    """
    try:
        STATE_FILE.write_text("null", encoding="utf-8")
    except Exception:
        pass


def wait_for_state(deadline_sec: float = 3.0) -> dict | None:
    end = time.monotonic() + deadline_sec
    while time.monotonic() < end:
        data = _read_json(STATE_FILE)
        if data is not None:
            return data
        time.sleep(0.01)
    return None


def _focus_piano_roll() -> None:
    """Tell the FL bridge to raise the piano roll window before we send the hotkey.

    Without this, Cmd+Opt+Y may land on the channel rack or another FL window
    and never reach the ComposeWithLLM script.
    """
    try:
        from .bridge_client import get_client
        get_client().call("ui.showWindow", name="piano_roll", focus=True)
        time.sleep(0.1)   # let FL finish raising the window
    except Exception:
        pass  # bridge offline — keystroke goes to whatever is focused


_AUTO_PICKUP_SEC = 2.0     # wait after the automated shortcut before prompting the user
_MANUAL_PICKUP_SEC = 60.0  # wait for a physical Cmd+Opt+Y after the prompt
_LATE_RECHECK_SEC = 1.0    # after withdrawing on timeout, watch for a run that started just before
_MANUAL_PROMPT_LINES = (
    "FL Studio didn't respond automatically.",
    "Click the Piano Roll title bar, then press Cmd+Option+Y.",
    "Waiting up to %d seconds." % _MANUAL_PICKUP_SEC,
)


def _open_prompt_dialog():
    """Show an informational dialog asking for a physical Cmd+Opt+Y.

    Non-blocking: returns the osascript process (or the launch exception) so the
    caller keeps polling for the staged request while the dialog is visible.
    """
    import subprocess
    text = " & linefeed & linefeed & ".join('"%s"' % line for line in _MANUAL_PROMPT_LINES)
    script = ('tell application "System Events" to display dialog (%s) with title "FL Studio MCP" '
              'buttons {"OK"} default button "OK" giving up after %d' % (text, _MANUAL_PICKUP_SEC + 5))
    try:
        return subprocess.Popen(["osascript", "-e", script],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except Exception as e:
        return e


def _close_prompt_dialog(proc) -> dict:
    """Close the dialog if it is still open and report what happened to it."""
    import subprocess
    if isinstance(proc, Exception):
        return {"launched": False, "error": repr(proc)}
    still_open = proc.poll() is None
    if still_open:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
    return {"launched": True, "dismissed_by_user": not still_open,
            "closed_automatically": still_open, "rc": proc.returncode, "stderr": err}


def _auto_pickup_window(actions: list[dict], wait_sec: float) -> float:
    """How long to wait for the automated shortcut before prompting the user.

    ComposeWithLLM only writes the state file after it has processed the whole
    request, so note-editing requests get the caller's (longer) wait_sec to avoid
    prompting while a large edit is still running. Read-only exports use the
    short window.
    """
    if all(a.get("action") == "export_only" for a in actions):
        return _AUTO_PICKUP_SEC
    return max(_AUTO_PICKUP_SEC, wait_sec)


def _await_pickup_mac(actions: list[dict], fired: bool, auto_wait_sec: float) -> tuple[dict | None, dict]:
    """Wait for ComposeWithLLM to consume the staged request.

    No automated retries and no restaging: if the automated shortcut isn't picked up
    within auto_wait_sec, show a non-blocking dialog asking for a physical
    Cmd+Opt+Y and wait up to _MANUAL_PICKUP_SEC for the same request. The dialog is
    closed when the wait ends. On timeout, withdraw the request.
    """
    pickup: dict = {"auto_shortcut_sent": fired, "auto_wait_sec": auto_wait_sec, "fallback": False}
    state = wait_for_state(auto_wait_sec) if fired else None
    if state is not None:
        pickup["picked_up_by"] = "automated_shortcut"
        return state, pickup

    pickup["fallback"] = True
    pickup["request_still_staged"] = _read_json(REQUEST_FILE) == actions
    pickup["prompt"] = "\n\n".join(_MANUAL_PROMPT_LINES)
    log.warning("automated Cmd+Opt+Y not picked up; waiting %.0fs for a physical press", _MANUAL_PICKUP_SEC)
    dialog = _open_prompt_dialog()
    t0 = time.monotonic()
    state = None
    try:
        state = wait_for_state(_MANUAL_PICKUP_SEC)
    finally:
        pickup["waited_sec"] = round(time.monotonic() - t0, 2)
        pickup["manual_wait_sec"] = _MANUAL_PICKUP_SEC
        if state is None:
            pickup["picked_up_by"] = None
            pickup["request_at_timeout"] = _read_json(REQUEST_FILE)
            # Withdraw the unconsumed request so a later stray Cmd+Opt+Y can't apply it.
            if pickup["request_at_timeout"] == actions:
                _write_json(REQUEST_FILE, [])
                pickup["request_withdrawn"] = True
            else:
                pickup["request_withdrawn"] = False
        else:
            pickup["picked_up_by"] = "after_manual_prompt"
        pickup["dialog"] = _close_prompt_dialog(dialog)
    if state is None:
        # A run that read the request just before the withdrawal still writes a state.
        # Its actions may have been applied, so report it rather than a clean failure.
        late = wait_for_state(_LATE_RECHECK_SEC)
        pickup["late_state_after_withdrawal"] = late is not None
        if late is not None:
            pickup["late_state"] = late
    return state, pickup


def stage_and_run(actions: list[dict], wait_sec: float = 3.0, focus_piano_roll: bool = True) -> dict:
    """Queue one or more piano-roll actions, fire the hotkey, wait for state."""
    from .keystroke import send_hotkey_mac, send_hotkey_windows

    if not is_installed():
        return {
            "ok": False,
            "error": (
                "ComposeWithLLM.pyscript is not installed. Run the installer "
                "or copy fl_bridge/piano_roll/ComposeWithLLM.pyscript into "
                f"{PR_DIR}"
            ),
        }

    clear_state()
    _write_json(REQUEST_FILE, actions)

    if focus_piano_roll and sys.platform != "darwin":
        # Raise the piano roll window inside FL before sending the hotkey so the
        # keystroke is guaranteed to reach the ComposeWithLLM script rather than
        # landing on the channel rack or mixer.
        _focus_piano_roll()

    if sys.platform == "darwin":
        # On macOS the piano-roll focus call runs inside send_hotkey_mac, after FL
        # is brought to the front and before the keystroke.
        fired = send_hotkey_mac(_focus_piano_roll if focus_piano_roll else None)
        state, pickup = _await_pickup_mac(actions, fired, _auto_pickup_window(actions, wait_sec))
        if state is None and pickup.get("late_state_after_withdrawal"):
            note = (
                "FL Studio did not pick up the request within the manual window, but a Piano "
                "Roll run finished just after it was withdrawn, so the actions may have been "
                "applied. Re-read the Piano Roll before retrying."
            )
        elif state is None:
            note = (
                "FL Studio did not pick up the automated Cmd+Opt+Y, and the request was not "
                f"consumed within {_MANUAL_PICKUP_SEC:.0f}s of the manual prompt. The staged "
                "request was withdrawn so it cannot run later; nothing was applied."
            )
        elif pickup["fallback"]:
            note = "FL Studio did not pick up the automated Cmd+Opt+Y; the request was consumed after the manual prompt."
        else:
            note = None
        return {
            "ok": state is not None,
            "hotkey_sent": fired,
            "staged_actions": len(actions),
            "state": state,
            "note": note,
            "pickup": pickup,
        }
    else:
        fired = send_hotkey_windows()
        manual_hint = "Could not auto-press Ctrl+Alt+Y. Make sure FL Studio is in the foreground and ComposeWithLLM is the active piano-roll script, then press Ctrl+Alt+Y manually."

    state = wait_for_state(wait_sec) if fired else None

    return {
        "ok": bool(fired and state is not None),
        "hotkey_sent": fired,
        "staged_actions": len(actions),
        "state": state,
        "note": None if fired else manual_hint,
    }


def read_state() -> dict | None:
    """Return the most recent state file without running anything."""
    return _read_json(STATE_FILE)
