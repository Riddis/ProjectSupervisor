"""NVIDIA GPU memory probe (nvidia-smi), shared by every extension or tool that needs to know what the card has
free before it starts something — a renderer lane's preflight, a doctor check, an overview panel, a benchmark.
Never raises: a missing or erroring nvidia-smi reads as "unknown" (None), so a machine without the driver on PATH
degrades to no preflight rather than a crash. Nothing here decides policy; routing.yaml and the extensions do."""
from __future__ import annotations

import subprocess

_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _query(field: str) -> int | None:
    try:
        out = subprocess.run(["nvidia-smi", f"--query-gpu={field}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, creationflags=_FLAGS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return int(out.stdout.splitlines()[0].strip())
    except ValueError:
        return None


def free_mb() -> int | None:
    return _query("memory.free")


def used_mb() -> int | None:
    return _query("memory.used")


def total_mb() -> int | None:
    return _query("memory.total")


def available() -> bool:
    """True when nvidia-smi answers at all (the driver is installed and on PATH)."""
    return total_mb() is not None
