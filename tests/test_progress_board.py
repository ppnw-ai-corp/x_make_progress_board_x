from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:  # pragma: no cover - typing helpers only
    from collections.abc import Sequence

import pytest

pytest.importorskip("PySide6")

from x_make_progress_board_x import cli, controller


def test_launch_board_in_thread_invokes_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text("{}", encoding="utf-8")

    observed: dict[str, object] = {}

    def fake_run_progress_board(
        *,
        snapshot_path: Path,
        stage_definitions: Sequence[tuple[str, str]],
        worker_done_event: threading.Event,
    ) -> None:
        observed["snapshot_path"] = snapshot_path
        observed["stage_definitions"] = list(stage_definitions)
        worker_done_event.set()

    monkeypatch.setattr(controller, "run_progress_board", fake_run_progress_board)

    worker_called: list[bool] = []

    def worker(event: threading.Event) -> None:
        worker_called.append(True)
        assert event.is_set() is False
        event.set()

    result = controller.launch_board_in_thread(
        snapshot_path=snapshot_path,
        stage_definitions=[("env", "Environment")],
        worker=worker,
    )
    result.thread.join(timeout=1)
    assert result.done_event.is_set() is True
    assert worker_called
    assert observed["snapshot_path"] == snapshot_path
    assert observed["stage_definitions"] == [("env", "Environment")]


def test_cli_main_invokes_progress_board(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    payload = {
        "stages": [
            {"id": "env", "title": "Environment"},
        ]
    }
    snapshot_path.write_text(json.dumps(payload), encoding="utf-8")

    observed: dict[str, object] = {}

    def fake_run_progress_board(
        *,
        snapshot_path: Path,
        stage_definitions: Sequence[tuple[str, str]],
        worker_done_event: threading.Event,
    ) -> None:
        observed["snapshot_path"] = snapshot_path
        observed["stage_definitions"] = list(stage_definitions)
        observed["event"] = worker_done_event
        worker_done_event.set()

    monkeypatch.setattr(cli, "run_progress_board", fake_run_progress_board)

    exit_code = cli.main(["--snapshot", str(snapshot_path)])
    assert exit_code == 0
    assert observed["snapshot_path"] == snapshot_path
    assert observed["stage_definitions"] == [("env", "Environment")]
    event = cast("threading.Event", observed["event"])
    assert event.is_set()
