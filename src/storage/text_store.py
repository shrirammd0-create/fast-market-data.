"""Append-only text log for snapshot blocks (market_updates.txt)."""

from __future__ import annotations

from pathlib import Path


def append_block(path: str | Path, block: str) -> Path:
    """Append one snapshot block, guaranteeing blank-line separation.

    Creates the file (and parent directories) on first write. Returns the
    resolved path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    block = block.rstrip("\n") + "\n"
    needs_separator = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8") as fh:
        if needs_separator:
            fh.write("\n")
        fh.write(block)
    return path
