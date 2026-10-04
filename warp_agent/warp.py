"""Drive the Warp app: warp:// links, tab configs, guarded keystrokes, screenshots.

Links need no permissions and are preferred. Keystrokes are used only where
Warp has no link (creating a tab group, splitting an existing pane); they need
Accessibility access for Warp and are guarded like Peter Hartree's pane skill:
wait for modifier keys to be released, and check before every step that Warp
is frontmost with the expected window title.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import time
import unicodedata
from pathlib import Path

PROCESS = "stable"  # Warp's process name as seen by System Events
TAB_CONFIG_DIR = Path.home() / ".warp" / "tab_configs"
TAB_CONFIG_PREFIX = "warp-agent-"
SAFE_TYPING = re.compile(r"^[A-Za-z0-9 /._;:=-]*$")


class WarpError(RuntimeError):
    pass


def open_url(url: str) -> None:
    subprocess.run(["open", url], check=True)


def _toml_string(value: str) -> str:
    return json.dumps(value)  # JSON strings are valid TOML basic strings


def write_tab_config(stem: str, title: str, panes: list[dict], split: str = "horizontal") -> Path:
    """Write a tab config with one or more terminal panes.

    Each pane dict has `directory` and `command`. Several panes become one
    split row (horizontal) or column (vertical).
    """
    lines = [f"name = {_toml_string(stem)}", f"title = {_toml_string(title)}", ""]
    if len(panes) > 1:
        children = ", ".join(_toml_string(f"p{i}") for i in range(len(panes)))
        lines += ["[[panes]]", 'id = "root"', f"split = {_toml_string(split)}",
                  f"children = [{children}]", ""]
    for index, pane in enumerate(panes):
        lines += [
            "[[panes]]",
            f"id = {_toml_string(f'p{index}')}",
            'type = "terminal"',
            f"directory = {_toml_string(pane['directory'])}",
            f"commands = [{_toml_string(pane['command'])}]",
        ]
        if index == 0:
            lines.append("is_focused = true")
        lines.append("")
    TAB_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = TAB_CONFIG_DIR / f"{stem}.toml"
    path.write_text("\n".join(lines))
    return path


def open_tab_config(stem: str, new_window: bool = False) -> None:
    open_url(f"warp://tab_config/{stem}" + ("?new_window=true" if new_window else ""))


def front_window_title() -> str | None:
    result = subprocess.run(
        ["osascript", "-e",
         f'tell application "System Events" to tell process "{PROCESS}" to '
         'if frontmost then get name of front window'],
        capture_output=True, text=True,
    )
    title = result.stdout.strip()
    return title or None


def wait_for_front_title(expected: str, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if front_window_title() == expected:
            return True
        time.sleep(0.1)
    return False


def focus(focus_url: str, expected_title: str | None = None, timeout: float = 3.0) -> bool:
    open_url(focus_url)
    if expected_title is None:
        return True
    return wait_for_front_title(expected_title, timeout)


# Keystrokes -----------------------------------------------------------------

_MODIFIER_MASK = (1 << 17) | (1 << 18) | (1 << 19) | (1 << 20)
_flags_state = None


def _modifier_flags() -> int:
    global _flags_state
    if _flags_state is None:
        services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        _flags_state = services.CGEventSourceFlagsState
        _flags_state.argtypes = [ctypes.c_int]
        _flags_state.restype = ctypes.c_uint64
    return int(_flags_state(0))


def wait_for_modifiers_released(timeout: float = 5.0, stable: float = 0.3) -> None:
    started = time.monotonic()
    released_at = None
    while time.monotonic() - started < timeout:
        if _modifier_flags() & _MODIFIER_MASK:
            released_at = None
        elif released_at is None:
            released_at = time.monotonic()
        elif time.monotonic() - released_at >= stable:
            return
        time.sleep(0.025)
    raise WarpError("modifier keys stayed held; stopped before sending keystrokes")


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def send_keys(steps: list[tuple], expected_title: str | None) -> None:
    """Send keystroke steps to Warp, checking focus before each one.

    Steps: ("key", "p", ["command"]), ("code", 36), ("text", "abc"), ("delay", 0.5).
    `expected_title` is checked before the first step only, because steps such
    as splitting a pane may legitimately change the window title.
    """
    wait_for_modifiers_released()
    guard = (f'if not frontmost of process "{PROCESS}" then error '
             '"Warp lost focus; stopped before sending keystrokes"')
    body = [guard]
    if expected_title is not None:
        body.append(
            f'if name of front window of process "{PROCESS}" is not '
            f'{_applescript_string(expected_title)} then error "wrong Warp window in front"')
    for step in steps:
        kind = step[0]
        if kind == "delay":
            body.append(f"delay {step[1]}")
            continue
        body.append(guard)
        if kind == "key":
            modifiers = ", ".join(f"{m} down" for m in step[2])
            using = f" using {{{modifiers}}}" if modifiers else ""
            body.append(f"keystroke {_applescript_string(step[1])}{using}")
        elif kind == "code":
            body.append(f"key code {step[1]}")
        elif kind == "text":
            if not SAFE_TYPING.match(step[1]):
                raise WarpError(f"refusing to type characters that depend on keyboard layout: {step[1]!r}")
            body.append(f"keystroke {_applescript_string(step[1])}")
    script = 'tell application "System Events"\n' + "\n".join(body) + "\nend tell"
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        raise WarpError(result.stderr.strip() or "keystroke automation failed")


RETURN = 36


KEYBINDINGS = Path.home() / ".warp" / "keybindings.yaml"
GROUP_ACTION = "workspace:new_tab_group_from_active_or_selected_tabs"
GROUP_CHORD = ("g", ["control", "option", "command"])  # ctrl-alt-cmd-g in keybindings.yaml


def _warp_started_at() -> float | None:
    out = subprocess.run(["ps", "-axo", "pid=,lstart=,comm="], capture_output=True, text=True,
                         env={**os.environ, "LC_ALL": "C"}).stdout
    for line in out.splitlines():
        if line.rstrip().endswith("/Warp.app/Contents/MacOS/stable"):
            started = " ".join(line.split()[1:6])
            return time.mktime(time.strptime(started, "%a %b %d %H:%M:%S %Y"))
    return None


def group_chord_loaded() -> bool:
    """True if keybindings.yaml binds the group action and Warp started after it was written."""
    try:
        if f'"{GROUP_ACTION}": ctrl-alt-cmd-g' not in KEYBINDINGS.read_text():
            return False
        written = KEYBINDINGS.resolve().stat().st_mtime
    except OSError:
        return False
    started = _warp_started_at()
    return started is not None and started > written


def create_group_from_active_tab(name: str, expected_title: str) -> None:
    """Put the active tab in a new tab group and name it.

    Uses the ctrl-alt-cmd-g binding when Warp has loaded it (about 0.5 s of
    keystrokes); otherwise the command palette (about 3 s).
    """
    if not SAFE_TYPING.match(name):
        raise WarpError(f"group names may use letters, digits, spaces and . _ - only: {name!r}")
    if group_chord_loaded():
        opening = [("key", GROUP_CHORD[0], GROUP_CHORD[1]), ("delay", 0.4)]
    else:
        opening = [("key", "p", ["command"]), ("delay", 0.6),
                   ("text", "Create tab group from active"), ("delay", 0.8),
                   ("code", RETURN), ("delay", 0.8)]
    send_keys(opening + [("key", "a", ["command"]), ("text", name), ("delay", 0.2),
                         ("code", RETURN)], expected_title)


SPLIT_SETTLE = float(os.environ.get("WARP_AGENT_SPLIT_SETTLE", "1.0"))


def split_and_run(command: str, expected_title: str, direction: str = "right") -> None:
    """Split the focused pane, wait for the new pane to take input, and run a command."""
    modifiers = ["command"] if direction == "right" else ["command", "shift"]
    send_keys([
        ("key", "d", modifiers), ("delay", SPLIT_SETTLE),
        ("text", command), ("code", RETURN),
    ], expected_title)


# Screenshots and text recognition ---------------------------------------------

_OCR_SOURCE = Path(__file__).with_name("ocr.swift")
_WINDOWS_SOURCE = Path(__file__).with_name("windows.swift")


def _compiled(source: Path) -> Path:
    from . import state
    binary = state.home() / "bin" / source.stem
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        binary.parent.mkdir(exist_ok=True)
        subprocess.run(["swiftc", "-O", "-o", str(binary), str(source)], check=True,
                       capture_output=True)
    return binary


def warp_windows() -> list[dict]:
    out = subprocess.run([str(_compiled(_WINDOWS_SOURCE))], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def screenshot(window_title: str, path: Path) -> float:
    """Capture one Warp window; return the pixels-per-point scale of the image."""
    for window in warp_windows():
        if window["title"] == window_title:
            subprocess.run(["screencapture", "-x", "-o", "-l", str(window["id"]), str(path)], check=True)
            with open(path, "rb") as handle:
                pixel_width = int.from_bytes(handle.read(24)[16:20], "big")  # PNG IHDR width
            return pixel_width / window["width"] if window.get("width") else 1.0
    raise WarpError(f"no Warp window titled {window_title!r}")


def recognize_text(image: Path) -> list[dict]:
    out = subprocess.run([str(_compiled(_OCR_SOURCE)), str(image)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


_CONFUSABLES = str.maketrans({"е": "e", "а": "a", "о": "o", "р": "p", "с": "c", "х": "x", "і": "i"})


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_CONFUSABLES).casefold()
    return re.sub(r"\s+", " ", text).strip()


def match_name(candidates, name: str, cutoff: float = 0.85) -> str | None:
    """Best fuzzy match for a name read by text recognition, which can drop or swap a character."""
    import difflib
    found = difflib.get_close_matches(normalize(name), list(candidates), n=1, cutoff=cutoff)
    return found[0] if found else None


def sidebar_visible(lines: list[dict], scale: float = 1.0) -> bool:
    """The vertical tabs panel shows a "Search tabs..." box at its top left."""
    return any(l["x"] < 120 * scale and "search tabs" in normalize(l["text"]) for l in lines)


def sidebar_groups(lines: list[dict], scale: float = 1.0) -> dict[str, list[str]]:
    """Read tab groups from the vertical tabs sidebar of a screenshot.

    A group header is a line followed by an "N tab(s)" line. Its members are the
    rows below it that are indented further than the header, until a row at or
    left of the header's indent. The sidebar's rows start within its first 200
    points; terminal text starts past the default 248-point sidebar width.
    """
    rows = sorted((l for l in lines if l["x"] < 200 * scale), key=lambda l: l["y"])
    is_count = lambda row: re.fullmatch(r"\d+ tabs?", normalize(row["text"])) is not None
    is_symbol = lambda row: len(re.sub(r"[^0-9a-z]", "", normalize(row["text"]))) < 2
    groups: dict[str, list[str]] = {}
    current = None
    header_x = 0
    for index, row in enumerate(rows):
        if is_count(row) or is_symbol(row):
            continue
        following = next((r for r in rows[index + 1:] if not is_symbol(r)), None)
        if following is not None and is_count(following):
            current = normalize(row["text"])
            header_x = row["x"]
            groups[current] = []
        elif current is not None:
            if row["x"] > header_x + 6 * scale:
                groups[current].append(normalize(row["text"]))
            else:
                current = None
    return groups
