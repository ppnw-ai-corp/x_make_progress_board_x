"""Controller helpers for launching the progress board."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing helpers only
    from pathlib import Path

from x_make_progress_board_x.progress_board_widget import run_progress_board

StageDefinitions = Sequence[tuple[str, str]]


@dataclass(slots=True)
class BoardLaunchResult:
    thread: threading.Thread
    done_event: threading.Event


def launch_board_in_thread(
    *,
    snapshot_path: Path,
    stage_definitions: StageDefinitions,
    worker: Callable[[threading.Event], None],
) -> BoardLaunchResult:
    """Launch the progress board supervising *worker* in a background thread."""

    done_event = threading.Event()

    def _worker_wrapper() -> None:
        try:
            worker(done_event)
        finally:
            done_event.set()

    thread = threading.Thread(target=_worker_wrapper, name="progress-board-worker")
    thread.start()

    run_progress_board(
        snapshot_path=snapshot_path,
        stage_definitions=stage_definitions,
        worker_done_event=done_event,
    )

    return BoardLaunchResult(thread=thread, done_event=done_event)
