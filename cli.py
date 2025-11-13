"""Command-line entry for observing snapshot progress."""

from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing helpers only
    from collections.abc import Sequence

from x_make_common_x.progress_snapshot import load_progress_snapshot
from x_make_progress_board_x.progress_board_widget import run_progress_board


def _current_stage_layout(snapshot_path: Path) -> list[tuple[str, str]]:
    snapshot = load_progress_snapshot(snapshot_path)
    if snapshot is None:
        return []
    stages: list[tuple[str, str]] = []
    for stage in snapshot.stages.values():
        stage_id = stage.stage_id.strip()
        if not stage_id:
            continue
        title = stage.title.strip() or stage_id
        stages.append((stage_id, title))
    return stages


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch the PySide6 progress board")
    parser.add_argument(
        "--snapshot", required=True, help="Path to progress snapshot JSON"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    snapshot_path = Path(args.snapshot).resolve()
    definitions = _current_stage_layout(snapshot_path)
    if not definitions:
        print("No stages reported yet; using default template.")
    worker_event = threading.Event()
    worker_event.set()
    run_progress_board(
        snapshot_path=snapshot_path,
        stage_definitions=definitions or [("environment", "Environment")],
        worker_done_event=worker_event,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
