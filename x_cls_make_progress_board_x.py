"""Canonical entry module for x_make_progress_board_x."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import IO, Protocol, Required, TypedDict, cast

from x_make_common_x.json_contracts import validate_payload
from x_make_common_x.progress_snapshot import load_progress_snapshot
from x_make_progress_board_x.json_contracts import (
    ERROR_SCHEMA,
    INPUT_SCHEMA,
    OUTPUT_SCHEMA,
)

try:  # pragma: no cover - optional runtime dependency
    from x_make_progress_board_x.progress_board_widget import (
        run_progress_board as _run_progress_board,
    )
except (ModuleNotFoundError, RuntimeError):
    DEFAULT_BOARD_RUNNER: BoardRunner | None = None  # pragma: no cover
else:
    DEFAULT_BOARD_RUNNER = _run_progress_board

SCHEMA_VERSION = "x_make_progress_board_x.run/1.0"

StageTuple = tuple[str, str]


class BoardRunner(Protocol):
    def __call__(
        self,
        *,
        snapshot_path: Path,
        stage_definitions: Sequence[StageTuple],
        worker_done_event: threading.Event,
    ) -> None: ...


class _SchemaValidationError(Exception):
    message: str
    path: tuple[object, ...]
    schema_path: tuple[object, ...]


class _JsonSchemaModule(Protocol):
    ValidationError: type[_SchemaValidationError]


def _load_validation_error() -> type[_SchemaValidationError]:
    module = cast("_JsonSchemaModule", importlib.import_module("jsonschema"))
    return module.ValidationError


ValidationErrorType: type[_SchemaValidationError] = _load_validation_error()


class StageSummary(TypedDict, total=False):
    id: str
    title: str


class PreviewPayload(TypedDict, total=False):
    stage_definitions: Required[list[StageTuple]]
    stage_count: Required[int]
    snapshot_exists: Required[bool]
    fallback_applied: Required[bool]
    snapshot_error: str


def _dedupe_preserve_order(items: Iterable[StageTuple]) -> list[StageTuple]:
    seen: set[StageTuple] = set()
    ordered: list[StageTuple] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _normalize_stage_entry(candidate: object) -> StageTuple | None:
    if isinstance(candidate, Mapping):
        stage_id_obj = candidate.get("id")
        title_obj = candidate.get("title")
        stage_id = str(stage_id_obj).strip() if isinstance(stage_id_obj, str) else ""
        if not stage_id:
            return None
        title = (
            str(title_obj).strip()
            if isinstance(title_obj, str) and title_obj.strip()
            else stage_id
        )
        return stage_id, title
    if isinstance(candidate, Sequence) and not isinstance(
        candidate, (str, bytes, bytearray)
    ):
        typed_candidate = cast("Sequence[object]", candidate)
        extracted = list(typed_candidate)
        if not extracted:
            return None
        stage_id = str(extracted[0]).strip()
        if not stage_id:
            return None
        title = (
            str(extracted[1]).strip()
            if len(extracted) > 1 and isinstance(extracted[1], str)
            else stage_id
        )
        return stage_id, title
    return None


def _normalize_stage_sequence(entries: object) -> list[StageTuple]:
    if not isinstance(entries, Sequence) or isinstance(
        entries, (str, bytes, bytearray)
    ):
        return []
    typed_entries = cast("Sequence[object]", entries)
    normalized: list[StageTuple] = []
    for entry in typed_entries:
        stage = _normalize_stage_entry(entry)
        if stage is not None:
            normalized.append(stage)
    return _dedupe_preserve_order(normalized)


def _normalize_single_stage(entry: object) -> StageTuple | None:
    stage = _normalize_stage_entry(entry)
    if stage is None:
        return None
    return stage


class XClsMakeProgressBoardX:
    """Coordinate stage discovery and board launch orchestration."""

    DEFAULT_SNAPSHOT = (
        Path(__file__).resolve().parent / "reports" / "make_all_progress.json"
    )
    DEFAULT_FALLBACK_STAGE: StageTuple = ("environment", "Environment")

    def __init__(
        self,
        *,
        snapshot_path: str | Path | None = None,
        stage_definitions: Sequence[StageTuple] | None = None,
        fallback_stage: StageTuple | None = None,
        runner: BoardRunner | None = None,
        ctx: object | None = None,
    ) -> None:
        snapshot_candidate = (
            Path(snapshot_path).expanduser() if snapshot_path else self.DEFAULT_SNAPSHOT
        )
        with suppress(OSError):
            snapshot_candidate = snapshot_candidate.resolve()
        self.snapshot_path: Path = snapshot_candidate
        provided: list[StageTuple] | None = None
        if stage_definitions is not None:
            provided = [
                (str(stage_id), str(title))
                for stage_id, title in stage_definitions
                if str(stage_id).strip()
            ]
        self._provided_stage_definitions = provided
        self._fallback_stage: StageTuple = fallback_stage or self.DEFAULT_FALLBACK_STAGE
        self._runner: BoardRunner | None = runner or DEFAULT_BOARD_RUNNER
        self._ctx = ctx
        self._resolved_stage_definitions: list[StageTuple] | None = None
        self._snapshot_exists: bool | None = None
        self._snapshot_error: str | None = None

    def _load_stage_definitions_from_snapshot(self) -> list[StageTuple]:
        path = self.snapshot_path
        exists = path.exists()
        self._snapshot_exists = exists
        if not exists:
            return []
        try:
            snapshot = load_progress_snapshot(path)
        except Exception as exc:  # noqa: BLE001 - convert to metadata later
            self._snapshot_error = str(exc)
            return []
        if snapshot is None:
            return []
        collected: list[StageTuple] = []
        for stage_id, stage in sorted(snapshot.stages.items()):
            candidate_id = str(stage_id).strip()
            if not candidate_id:
                continue
            title = stage.title.strip() or candidate_id
            collected.append((candidate_id, title))
        return collected

    def _resolve_stage_definitions(self) -> list[StageTuple]:
        if self._resolved_stage_definitions is not None:
            return self._resolved_stage_definitions
        if self._provided_stage_definitions:
            resolved = _dedupe_preserve_order(
                (str(stage_id), str(title))
                for stage_id, title in self._provided_stage_definitions
                if str(stage_id).strip()
            )
            if self._snapshot_exists is None:
                self._snapshot_exists = self.snapshot_path.exists()
            self._resolved_stage_definitions = resolved
            return resolved
        resolved = self._load_stage_definitions_from_snapshot()
        self._resolved_stage_definitions = resolved
        return resolved

    def _effective_stage_definitions(self) -> list[StageTuple]:
        resolved = self._resolve_stage_definitions()
        if resolved:
            return resolved
        return [self._fallback_stage]

    def preview(self) -> PreviewPayload:
        resolved = list(self._resolve_stage_definitions())
        effective = resolved if resolved else [self._fallback_stage]
        summary: PreviewPayload = {
            "stage_definitions": effective,
            "stage_count": len(effective),
            "snapshot_exists": bool(self._snapshot_exists),
            "fallback_applied": not resolved,
        }
        if self._snapshot_error:
            summary["snapshot_error"] = self._snapshot_error
        return summary

    def launch(
        self, *, worker: Callable[[threading.Event], None] | None = None
    ) -> dict[str, object]:
        runner = self._runner
        if runner is None:
            message = (
                "Progress board runner unavailable; install PySide6 to launch "
                "the board."
            )
            raise RuntimeError(message)
        stage_definitions = self._effective_stage_definitions()
        worker_error: Exception | None = None
        worker_done_event = threading.Event()
        worker_thread: threading.Thread | None = None
        if worker is None:
            worker_done_event.set()
        else:

            def _worker_wrapper() -> None:
                nonlocal worker_error
                try:
                    worker(worker_done_event)
                except Exception as exc:  # noqa: BLE001 - capture for metadata
                    worker_error = exc
                finally:
                    worker_done_event.set()

            worker_thread = threading.Thread(
                target=_worker_wrapper,
                name="progress-board-worker",
                daemon=True,
            )
            worker_thread.start()
        try:
            runner(
                snapshot_path=self.snapshot_path,
                stage_definitions=stage_definitions,
                worker_done_event=worker_done_event,
            )
        finally:
            if worker_thread is not None:
                worker_thread.join(timeout=5)
        metadata: dict[str, object] = {
            "launched": True,
            "stage_count": len(stage_definitions),
            "snapshot_path": str(self.snapshot_path),
            "worker_attached": worker is not None,
            "fallback_applied": not bool(self._resolve_stage_definitions()),
        }
        if worker_error is not None:
            metadata["worker_error"] = str(worker_error)
        return metadata


def _failure_payload(
    message: str, *, details: Mapping[str, object] | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {"status": "failure", "message": message}
    if details:
        payload["details"] = dict(details)
    with suppress(ValidationErrorType):
        validate_payload(payload, ERROR_SCHEMA)
    return payload


_EMPTY_MAPPING: Mapping[str, object] = {}


def _coerce_mapping(obj: object) -> Mapping[str, object]:
    if isinstance(obj, Mapping):
        return cast("Mapping[str, object]", obj)
    return _EMPTY_MAPPING


def _load_json_payload(file_path: str | None) -> Mapping[str, object]:
    def _load(handle: IO[str]) -> Mapping[str, object]:
        payload_obj: object = json.load(handle)
        if not isinstance(payload_obj, Mapping):
            message = "JSON payload must be an object"
            raise TypeError(message)
        return cast("Mapping[str, object]", payload_obj)

    if file_path:
        with Path(file_path).open("r", encoding="utf-8") as handle:
            return _load(handle)
    return _load(sys.stdin)


def main_json(
    payload: Mapping[str, object], *, ctx: object | None = None
) -> dict[str, object]:
    try:
        validate_payload(payload, INPUT_SCHEMA)
    except ValidationErrorType as exc:
        return _failure_payload(
            "input payload failed validation",
            details={
                "error": exc.message,
                "path": [str(part) for part in exc.path],
                "schema_path": [str(part) for part in exc.schema_path],
            },
        )

    parameters = _coerce_mapping(payload.get("parameters"))
    snapshot_path_obj = parameters.get("snapshot_path")
    snapshot_path = (
        str(snapshot_path_obj)
        if isinstance(snapshot_path_obj, str) and snapshot_path_obj.strip()
        else None
    )
    stage_defs_raw = parameters.get("stage_definitions")
    stage_definitions = _normalize_stage_sequence(stage_defs_raw)
    fallback_stage = _normalize_single_stage(parameters.get("fallback_stage"))
    launch_flag = bool(parameters.get("launch", False))

    board = XClsMakeProgressBoardX(
        snapshot_path=snapshot_path,
        stage_definitions=stage_definitions if stage_definitions else None,
        fallback_stage=fallback_stage,
        ctx=ctx,
    )

    preview = board.preview()
    stage_defs_preview = preview["stage_definitions"]
    stage_def_summaries: list[StageSummary] = [
        {"id": stage_id, "title": title} for stage_id, title in stage_defs_preview
    ]
    stage_count = preview["stage_count"]
    snapshot_exists = preview["snapshot_exists"]
    fallback_applied = preview["fallback_applied"]
    metadata: dict[str, object] = {
        "stage_count": stage_count,
        "snapshot_exists": snapshot_exists,
        "fallback_applied": fallback_applied,
        "launched": False,
    }
    snapshot_error = preview.get("snapshot_error")
    if isinstance(snapshot_error, str) and snapshot_error:
        metadata["snapshot_error"] = snapshot_error

    message = "Progress board staged"
    if launch_flag:
        try:
            launch_meta = board.launch()
        except Exception as exc:  # noqa: BLE001 - convert to JSON failure payload
            return _failure_payload(
                "progress board launch failed",
                details={"error": str(exc)},
            )
        metadata.update(launch_meta)
        message = "Progress board launched"
    result_payload: dict[str, object] = {
        "status": "success",
        "schema_version": SCHEMA_VERSION,
        "message": message,
        "snapshot_path": str(board.snapshot_path),
        "stage_definitions": stage_def_summaries,
        "metadata": metadata,
    }
    try:
        validate_payload(result_payload, OUTPUT_SCHEMA)
    except ValidationErrorType as exc:
        return _failure_payload(
            "generated output failed schema validation",
            details={
                "error": exc.message,
                "path": [str(part) for part in exc.path],
                "schema_path": [str(part) for part in exc.schema_path],
            },
        )
    return result_payload


class _JsonCliArgs(argparse.Namespace):
    json: bool
    json_file: str | None


def _run_json_cli(args: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(description="x_make_progress_board_x JSON runner")
    parser.add_argument(
        "--json", action="store_true", help="Read JSON payload from stdin"
    )
    parser.add_argument("--json-file", type=str, help="Path to JSON payload file")
    parsed: _JsonCliArgs = parser.parse_args(list(args), namespace=_JsonCliArgs())
    json_flag = parsed.json
    json_file = parsed.json_file
    if not (json_flag or json_file):
        parser.error("JSON input required. Use --json for stdin or --json-file <path>.")
    payload = _load_json_payload(json_file)
    result = main_json(payload)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    cli_module = importlib.import_module("x_make_progress_board_x.cli")
    cli_main = cast(
        "Callable[[Sequence[str] | None], int]",
        cli_module.main,
    )
    return cli_main(argv)


x_cls_make_progress_board_x = XClsMakeProgressBoardX

__all__ = [
    "SCHEMA_VERSION",
    "XClsMakeProgressBoardX",
    "main",
    "main_json",
    "x_cls_make_progress_board_x",
]


if __name__ == "__main__":  # pragma: no cover - manual execution helper
    import sys

    if "--json" in sys.argv or "--json-file" in sys.argv:
        _run_json_cli(sys.argv[1:])
    else:
        raise SystemExit(main(sys.argv[1:]))
