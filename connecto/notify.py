"""Desktop notifications so an unattended run can tell you when it needs you. Best effort: never raises, never blocks."""
from __future__ import annotations

import base64
import shutil
import subprocess
import sys

MAX_TITLE = 80
MAX_BODY = 240


def _clean(s: str, n: int) -> str:
    s = "".join(ch if ch.isprintable() else " " for ch in str(s)).strip()
    return s[:n]


def build_command(title: str, message: str, platform: str | None = None) -> list[str] | None:
    """The command line that shows a notification on this OS, or None if there is no known way. Pure, so testable."""
    platform = platform or sys.platform
    title, message = _clean(title, MAX_TITLE), _clean(message, MAX_BODY)
    if platform == "darwin":
        esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
        return ["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"']
    if platform.startswith("linux"):
        return ["notify-send", "--app-name=Connecto", title, message]
    if platform.startswith("win"):
        # A tray balloon via .NET, passed as -EncodedCommand (UTF-16LE base64) so nothing needs shell quoting.
        q = lambda s: s.replace("'", "''")  # noqa: E731
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
            f"$n.ShowBalloonTip(8000, '{q(title)}', '{q(message)}', 'Info'); Start-Sleep -Seconds 9; $n.Dispose()"
        )
        enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", enc]
    return None


def notify(title: str, message: str, enabled: bool = True) -> bool:
    if not enabled:
        return False
    cmd = build_command(title, message)
    if not cmd or shutil.which(cmd[0]) is None:
        return False
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        return True
    except Exception:
        return False
