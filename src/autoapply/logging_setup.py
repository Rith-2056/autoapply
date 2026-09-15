"""Per-run log file under logs/ plus a rich console handler."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from rich.logging import RichHandler


def setup_logging(logs_dir: Path, run_id: str, verbose: bool = False) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"run_{run_id}.log"
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    ch = RichHandler(rich_tracebacks=False, show_path=False, markup=False)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(ch)
    # Quieten noisy libraries
    for name in ("httpx", "httpcore", "anthropic", "urllib3", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return log_path


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")
