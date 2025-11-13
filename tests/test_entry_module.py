from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:  # pragma: no cover - typing helpers only
    from collections.abc import Sequence

from x_make_common_x.progress_snapshot import (
    create_progress_snapshot,
    write_progress_snapshot,
)
from x_make_progress_board_x.x_cls_make_progress_board_x import (
    StageSummary,
    XClsMakeProgressBoardX,
    main_json,
)


def _read_stage_ids(stage_definitions: Sequence[StageSummary]) -> list[str]:
    return [str(entry["id"]) for entry in stage_definitions if "id" in entry]


def test_main_json_uses_snapshot_when_available(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "progress.json"
    snapshot = create_progress_snapshot([("alpha", "Alpha"), ("beta", "Beta")])
    write_progress_snapshot(snapshot_path, snapshot)

    payload = {
        "command": "x_make_progress_board_x",
        "parameters": {"snapshot_path": str(snapshot_path)},
    }

    result = main_json(payload)
    assert isinstance(result["snapshot_path"], str)
    assert result["status"] == "success"
    assert result["schema_version"] == "x_make_progress_board_x.run/1.0"
    assert Path(result["snapshot_path"]) == snapshot_path

    stage_defs_obj = result["stage_definitions"]
    assert isinstance(stage_defs_obj, list)
    stage_defs = [cast("StageSummary", entry) for entry in stage_defs_obj]
    assert _read_stage_ids(stage_defs) == ["alpha", "beta"]
    assert "title" in stage_defs[0]
    assert stage_defs[0]["title"] == "Alpha"

    metadata_obj = result["metadata"]
    assert isinstance(metadata_obj, Mapping)
    metadata = cast("Mapping[str, object]", metadata_obj)
    assert metadata["snapshot_exists"] is True
    assert metadata["fallback_applied"] is False
    assert metadata["launched"] is False
    assert metadata["stage_count"] == 2


def test_main_json_applies_fallback_when_snapshot_missing(tmp_path: Path) -> None:
    missing_snapshot = tmp_path / "missing.json"
    payload = {
        "command": "x_make_progress_board_x",
        "parameters": {"snapshot_path": str(missing_snapshot)},
    }

    result = main_json(payload)
    assert result["status"] == "success"
    stage_defs_obj = result["stage_definitions"]
    assert isinstance(stage_defs_obj, list)
    stage_defs = [cast("StageSummary", entry) for entry in stage_defs_obj]
    assert len(stage_defs) == 1
    stage_def = stage_defs[0]
    assert "id" in stage_def
    assert stage_def["id"] == "environment"
    assert stage_def.get("title") == "Environment"

    metadata_obj = result["metadata"]
    assert isinstance(metadata_obj, Mapping)
    metadata = cast("Mapping[str, object]", metadata_obj)
    assert metadata["snapshot_exists"] is False
    assert metadata["fallback_applied"] is True
    assert metadata["launched"] is False
    assert metadata["stage_count"] == 1


def test_launch_with_injected_runner_and_worker(tmp_path: Path) -> None:
    stage_definitions = [("alpha", "Alpha"), ("beta", "Beta")]
    observed: dict[str, object] = {}

    def fake_runner(
        *,
        snapshot_path: Path,
        stage_definitions: Sequence[tuple[str, str]],
        worker_done_event: threading.Event,
    ) -> None:
        observed["snapshot_path"] = snapshot_path
        observed["stage_definitions"] = list(stage_definitions)
        observed["event_initial_state"] = worker_done_event.is_set()
        worker_done_event.set()

    worker_events: list[threading.Event] = []

    def fake_worker(event: threading.Event) -> None:
        worker_events.append(event)
        assert event.is_set() is False
        event.set()

    board = XClsMakeProgressBoardX(
        snapshot_path=tmp_path / "unused.json",
        stage_definitions=stage_definitions,
        runner=fake_runner,
    )

    metadata = board.launch(worker=fake_worker)

    assert observed["snapshot_path"] == board.snapshot_path
    assert observed["stage_definitions"] == stage_definitions
    assert observed["event_initial_state"] is False
    assert worker_events and worker_events[0].is_set()

    assert metadata["launched"] is True
    assert metadata["worker_attached"] is True
    assert metadata["stage_count"] == len(stage_definitions)
    assert metadata["fallback_applied"] is False
    assert "worker_error" not in metadata


def test_main_json_invalid_payload_returns_failure() -> None:
    result = main_json({"command": "unexpected"})
    assert result["status"] == "failure"
    assert "input payload failed validation" in cast("str", result["message"])
