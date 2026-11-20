"""Windows toast notifications that open the Snipr page when clicked.

Uses the notification API through PowerShell, so there is nothing to install.
A notification that cannot be shown is never an error for the caller.
"""

from __future__ import annotations

import os
import subprocess
import sys
from xml.sax.saxutils import escape

from . import config

# Toasts need an application identity. PowerShell's own one is always registered.
_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_SCRIPT = """
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($env:SNIPR_TOAST_XML)
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:SNIPR_TOAST_APP).Show($toast)
"""


def base_url() -> str:
    server = config.load_settings()["server"]
    return f"http://{server['host']}:{server['port']}"


def toast(title: str, body: str, route: str = "") -> bool:
    """Show a notification. Returns False when it could not be shown."""
    if sys.platform != "win32" or os.environ.get("SNIPR_NO_NOTIFY"):
        return False
    link = escape(f"{base_url()}/{route}", {'"': "&quot;"})
    xml = (f'<toast activationType="protocol" launch="{link}"><visual><binding template="ToastGeneric">'
           f"<text>{escape(title)}</text><text>{escape(body)}</text></binding></visual></toast>")
    env = dict(os.environ, SNIPR_TOAST_XML=xml, SNIPR_TOAST_APP=_APP_ID)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _SCRIPT],
            env=env, capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
