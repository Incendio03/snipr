"""Make the CUDA libraries from the ``nvidia-*`` wheels loadable on Windows.

CTranslate2 (under faster-whisper) loads cuBLAS and cuDNN by DLL name. The pip
wheels put them in ``site-packages/nvidia/<lib>/bin``, which Windows does not
search by default.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_done = False


def enable_cuda_dlls() -> list[str]:
    """Add the wheel DLL folders to the search path. Returns the folders added."""
    global _done
    added: list[str] = []
    if _done or sys.platform != "win32":
        return added
    for entry in sys.path:
        root = Path(entry) / "nvidia"
        if not root.is_dir():
            continue
        for bin_dir in root.glob("*/bin"):
            if any(bin_dir.glob("*.dll")):
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
                added.append(str(bin_dir))
    _done = True
    return added
