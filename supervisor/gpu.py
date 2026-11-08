"""NVIDIA GPU memory probe (nvidia-smi), shared by every plugin or tool that needs to know what a card has
free before it starts something — a renderer lane's preflight, a doctor check, an overview panel, a benchmark.
Every function takes an optional device index (None, the default, asks nvidia-smi for the first row, exactly as
before); with an index, `-i <index>` is inserted and the card's own row is read. Never raises: a missing or
erroring nvidia-smi, or an index that names no card, reads as "unknown" (None), so a machine without the driver
on PATH, or a mistyped index, degrades to no preflight rather than a crash. Nothing here decides policy or
validates a device name — routing.yaml and `supervisor.plugin.gpu_device` do."""
from __future__ import annotations

import subprocess

_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _query(field: str, index: int | None) -> int | None:
    argv = ["nvidia-smi"]
    if index is not None:
        argv += ["-i", str(index)]
    argv += [f"--query-gpu={field}", "--format=csv,noheader,nounits"]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=10, creationflags=_FLAGS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return int(out.stdout.splitlines()[0].strip())
    except ValueError:
        return None


def free_mb(index: int | None = None) -> int | None:
    return _query("memory.free", index)


def used_mb(index: int | None = None) -> int | None:
    return _query("memory.used", index)


def total_mb(index: int | None = None) -> int | None:
    return _query("memory.total", index)


def available(index: int | None = None) -> bool:
    """True when nvidia-smi answers at all for this index (the driver is installed, on PATH, and that card exists)."""
    return total_mb(index) is not None
