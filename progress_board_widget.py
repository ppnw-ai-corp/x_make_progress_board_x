"""Progress board widget extracted for packaging."""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:  # pragma: no cover - typing helpers only
    from collections.abc import Sequence

from x_make_common_x.progress_snapshot import (
    ProgressSnapshot,
    ProgressStage,
    load_progress_snapshot,
)

try:  # pragma: no cover - runtime import guard for UI toolkit
    from PySide6 import QtCore, QtWidgets
except ModuleNotFoundError as exc:  # pragma: no cover - surfaced to caller
    message = "PySide6 is required to display the progress board."
    raise RuntimeError(message) from exc

Qt = QtCore.Qt
QTimer = QtCore.QTimer
Signal = QtCore.Signal
_DONE_STATUSES = {"completed", "attention", "blocked"}
_POLL_INTERVAL_MS = 500
_AUTO_CLOSE_DELAY_MS = 750


class _CloseEvent(Protocol):
    """Marker protocol for Qt close events."""


@dataclass(slots=True)
class _RepoEntry:
    repo_id: str
    display_name: str
    status: str
    updated_at: str
    messages: tuple[str, ...]
    detail_path: str | None


@dataclass(slots=True)
class _RepoIndexCacheEntry:
    path: Path
    mtime: float | None
    entries: list[_RepoEntry]


class ProgressBoardWidget(QtWidgets.QWidget):
    """Checklist panel mirroring orchestrator stage progress."""

    board_completed = Signal()

    def __init__(
        self,
        *,
        snapshot_path: Path,
        stage_definitions: Sequence[tuple[str, str]],
        worker_done_event: threading.Event,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._snapshot_path = Path(snapshot_path)
        self._worker_done_event = worker_done_event
        self._stage_definitions: list[tuple[str, str]] = []
        for stage_id, title in stage_definitions:
            self._record_stage_definition(str(stage_id), str(title))

        self._items: dict[str, QtWidgets.QListWidgetItem] = {}
        self._repo_index_cache: dict[str, _RepoIndexCacheEntry] = {}
        self._selected_stage_id: str | None = None
        self._completion_triggered = False

        self._status_label: QtWidgets.QLabel
        self._checklist: QtWidgets.QListWidget
        self._detail_table: QtWidgets.QTableWidget

        self._build_ui()

        self._timer: QtCore.QTimer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh_snapshot)
        self._timer.start()

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QLabel("Initializing orchestration tooling...")
        header.setWordWrap(True)
        header.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._status_label = header
        layout.addWidget(header)

        splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal, self)
        layout.addWidget(splitter, stretch=1)

        stage_container = QtWidgets.QWidget(splitter)
        stage_layout = QtWidgets.QVBoxLayout(stage_container)
        stage_layout.setContentsMargins(0, 0, 0, 0)

        checklist = QtWidgets.QListWidget(stage_container)
        checklist.setAlternatingRowColors(True)
        checklist.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
        )
        checklist.itemSelectionChanged.connect(self._handle_stage_selection)
        stage_layout.addWidget(checklist)
        self._checklist = checklist

        splitter.addWidget(stage_container)

        detail_container = QtWidgets.QWidget(splitter)
        detail_layout = QtWidgets.QVBoxLayout(detail_container)
        detail_layout.setContentsMargins(0, 0, 0, 0)

        detail_label = QtWidgets.QLabel("Repository progress")
        detail_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        detail_layout.addWidget(detail_label)

        detail_table = QtWidgets.QTableWidget(parent=detail_container)
        detail_table.setColumnCount(4)
        detail_table.setHorizontalHeaderLabels(
            ["Repository", "Status", "Updated", "Messages"]
        )
        detail_table.horizontalHeader().setStretchLastSection(True)
        detail_table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        detail_table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        detail_table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection
        )
        detail_layout.addWidget(detail_table)
        self._detail_table = detail_table

        splitter.addWidget(detail_container)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        for stage_id, title in self._stage_definitions:
            self._ensure_stage_item(stage_id, title)

        self.setMinimumSize(640, 480)
        if self._checklist.count():
            first_item = self._checklist.item(0)
            if first_item is not None:
                self._checklist.setCurrentItem(first_item)
                self._selected_stage_id = str(first_item.data(Qt.ItemDataRole.UserRole))

    def _record_stage_definition(self, stage_id: str, title: str) -> None:
        for index, (existing_id, _) in enumerate(self._stage_definitions):
            if existing_id == stage_id:
                self._stage_definitions[index] = (stage_id, title)
                return
        self._stage_definitions.append((stage_id, title))

    def _ensure_stage_item(
        self,
        stage_id: str,
        title: str,
    ) -> QtWidgets.QListWidgetItem:
        existing = self._items.get(stage_id)
        if existing is not None:
            return existing
        item = QtWidgets.QListWidgetItem(f"{title} - pending")
        item.setData(Qt.ItemDataRole.UserRole, stage_id)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        item.setCheckState(Qt.CheckState.Unchecked)
        self._checklist.addItem(item)
        self._items[stage_id] = item
        return item

    def _refresh_snapshot(self) -> None:
        snapshot = load_progress_snapshot(self._snapshot_path)
        if snapshot is None:
            if not self._completion_triggered:
                self._status_label.setText("Waiting for progress snapshot feed...")
            return

        self._update_from_snapshot(snapshot)
        if self._worker_done_event.is_set() and not self._completion_triggered:
            self._status_label.setText("Tooling finished. Command center unlocking...")
            self._handle_completion()

    def _update_from_snapshot(self, snapshot: ProgressSnapshot) -> None:
        stages: dict[str, ProgressStage] = dict(snapshot.stages)
        for stage_id, stage in stages.items():
            self._record_stage_definition(stage_id, stage.title)
            self._ensure_stage_item(stage_id, stage.title)

        all_done = True
        for index, (stage_id, stored_title) in enumerate(self._stage_definitions):
            item = self._items.get(stage_id)
            if item is None:
                continue
            stage_obj = stages.get(stage_id)
            title = stage_obj.title if stage_obj is not None else stored_title
            if stage_obj is not None and stage_obj.title != stored_title:
                self._stage_definitions[index] = (stage_id, stage_obj.title)
            status = stage_obj.status if stage_obj is not None else "pending"
            messages = stage_obj.messages if stage_obj is not None else ()
            if not self._apply_stage_state(item, title, status, messages):
                all_done = False

        self._refresh_stage_repo_details(stages)
        self._update_detail_view(self._current_stage_id())

        if stages and not self._completion_triggered:
            if all_done:
                self._status_label.setText(
                    "All stages reported. Waiting for tooling shutdown..."
                )
            else:
                self._status_label.setText("Tracking orchestration stages...")

    def _apply_stage_state(
        self,
        item: QtWidgets.QListWidgetItem,
        title: str,
        status: str,
        messages: Sequence[str],
    ) -> bool:
        status_text = status or "pending"
        message_suffix = self._message_suffix(messages)
        item.setText(f"{title} - {status_text}{message_suffix}")

        normalized_status = status_text.lower()
        item.setCheckState(self._check_state_for_status(normalized_status))
        return normalized_status in _DONE_STATUSES

    def _handle_stage_selection(self) -> None:
        stage_id = self._current_stage_id()
        self._selected_stage_id = stage_id
        self._update_detail_view(stage_id)

    def _current_stage_id(self) -> str | None:
        selected = self._checklist.selectedItems()
        if not selected:
            return None
        return str(selected[0].data(Qt.ItemDataRole.UserRole))

    def _refresh_stage_repo_details(
        self,
        stages: Mapping[str, ProgressStage],
    ) -> None:
        observed_ids: set[str] = set()
        for stage_id, stage in stages.items():
            observed_ids.add(stage_id)
            cache_entry = self._load_repo_index_payload(stage_id, stage.metadata)
            if cache_entry is None:
                self._repo_index_cache.pop(stage_id, None)
            else:
                self._repo_index_cache[stage_id] = cache_entry

        self._prune_stale_repo_cache(observed_ids)

    def _update_detail_view(self, stage_id: str | None) -> None:
        table = self._detail_table
        if stage_id is None:
            table.setRowCount(0)
            return

        cache_entry = self._repo_index_cache.get(stage_id)
        if cache_entry is None:
            table.setRowCount(0)
            return

        entries = cache_entry.entries
        table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            display = entry.display_name or entry.repo_id or "<repo>"
            message_text = " | ".join(msg for msg in entry.messages if msg)

            table.setItem(row, 0, QtWidgets.QTableWidgetItem(display))
            table.setItem(row, 1, QtWidgets.QTableWidgetItem(entry.status))
            table.setItem(row, 2, QtWidgets.QTableWidgetItem(entry.updated_at))
            message_item = QtWidgets.QTableWidgetItem(message_text)
            if entry.detail_path is not None:
                message_item.setData(Qt.ItemDataRole.ToolTipRole, entry.detail_path)
            table.setItem(row, 3, message_item)

        table.resizeRowsToContents()

    def _handle_completion(self) -> None:
        if self._completion_triggered:
            return
        self._completion_triggered = True
        self._timer.stop()
        self.board_completed.emit()

    def closeEvent(self, event: _CloseEvent) -> None:  # noqa: N802 - Qt signature
        self._timer.stop()
        super().closeEvent(event)

    @staticmethod
    def _message_suffix(messages: Sequence[str]) -> str:
        for message in reversed(tuple(messages)):
            text = str(message).strip()
            if text:
                return f" ({text})"
        return ""

    @staticmethod
    def _check_state_for_status(status: str) -> int:
        normalized = status.lower().strip()
        if normalized in _DONE_STATUSES:
            return Qt.CheckState.Checked
        if normalized == "running":
            return Qt.CheckState.PartiallyChecked
        if normalized == "pending":
            return Qt.CheckState.Unchecked
        return Qt.CheckState.PartiallyChecked

    @staticmethod
    def _normalized_messages(messages_raw: object) -> tuple[str, ...]:
        if isinstance(messages_raw, Sequence) and not isinstance(
            messages_raw, (str, bytes, bytearray)
        ):
            normalized: list[str] = []
            for msg in messages_raw:
                text = str(msg).strip()
                if text:
                    normalized.append(text)
            return tuple(normalized)
        if isinstance(messages_raw, str):
            text = messages_raw.strip()
            if text:
                return (text,)
        return ()

    def _normalize_repo_entry(
        self,
        entry: Mapping[str, object],
        entries_dir: Path,
    ) -> _RepoEntry | None:
        repo_id = str(entry.get("repo_id") or "")
        display = str(entry.get("display_name") or repo_id or "<repo>")
        status = str(entry.get("status") or "pending")
        updated_at = str(entry.get("updated_at") or "")
        message_preview = self._normalized_messages(entry.get("message_preview"))
        detail_path_obj = entry.get("detail_path")
        detail_path: str | None = None
        if isinstance(detail_path_obj, str) and detail_path_obj:
            detail_path = str((entries_dir / detail_path_obj).resolve())
        return _RepoEntry(
            repo_id=repo_id,
            display_name=display,
            status=status,
            updated_at=updated_at,
            messages=message_preview,
            detail_path=detail_path,
        )

    def _normalize_repo_entries(
        self,
        entries_payload: object,
        entries_dir: Path,
    ) -> list[_RepoEntry]:
        normalized_entries: list[_RepoEntry] = []
        if isinstance(entries_payload, Sequence) and not isinstance(
            entries_payload, (str, bytes, bytearray)
        ):
            for entry in entries_payload:
                if isinstance(entry, Mapping):
                    normalized_entry = self._normalize_repo_entry(entry, entries_dir)
                    if normalized_entry is not None:
                        normalized_entries.append(normalized_entry)
        return normalized_entries

    @staticmethod
    def _safe_stat(path: Path) -> os.stat_result | None:
        try:
            return path.stat()
        except OSError:
            return None

    @staticmethod
    def _read_json_payload(path: Path) -> dict[str, object] | None:
        try:
            raw_payload = cast("object", json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw_payload, Mapping):
            return None
        mapping_payload = cast("Mapping[object, object]", raw_payload)
        normalized_payload: dict[str, object] = {
            str(key): value for key, value in mapping_payload.items()
        }
        return normalized_payload

    def _load_repo_index_payload(
        self,
        stage_id: str,
        metadata: Mapping[str, object] | None,
    ) -> _RepoIndexCacheEntry | None:
        metadata_dict: dict[str, object]
        if metadata is None:
            metadata_dict = {}
        else:
            metadata_dict = {str(key): value for key, value in metadata.items()}
        index_path_obj = metadata_dict.get("repo_progress_index_path")
        if not index_path_obj:
            return None
        index_path = Path(str(index_path_obj))
        stat_result = self._safe_stat(index_path)
        if stat_result is None:
            return None
        cached = self._repo_index_cache.get(stage_id)
        mtime: float | None = float(stat_result.st_mtime)
        if cached is not None and cached.mtime == mtime:
            return cached
        payload = self._read_json_payload(index_path)
        if payload is None:
            return None
        entries_dir = index_path.parent
        if "entries_dir" in payload:
            entries_dir_raw = payload["entries_dir"]
            if isinstance(entries_dir_raw, str):
                entries_dir_candidate = entries_dir_raw.strip()
                if entries_dir_candidate:
                    entries_dir = Path(entries_dir_candidate)
        entries_payload: object = payload["entries"] if "entries" in payload else None
        entries = self._normalize_repo_entries(entries_payload, entries_dir)
        return _RepoIndexCacheEntry(path=index_path, mtime=mtime, entries=entries)

    def _prune_stale_repo_cache(self, observed_ids: set[str]) -> None:
        stale_keys = [
            stage_id
            for stage_id in list(self._repo_index_cache)
            if stage_id not in observed_ids
        ]
        for stage_id in stale_keys:
            self._repo_index_cache.pop(stage_id, None)


def run_progress_board(
    *,
    snapshot_path: Path,
    stage_definitions: Sequence[tuple[str, str]],
    worker_done_event: threading.Event,
) -> None:
    """Display the progress board until the orchestrator worker finishes."""

    app = QtWidgets.QApplication.instance()
    created_app = False
    if app is None:
        app = QtWidgets.QApplication(sys.argv or ["x_make_progress_board_x"])
        created_app = True

    window = QtWidgets.QMainWindow()
    window.setWindowTitle("x_make_progress_board_x - Progress Board")

    board = ProgressBoardWidget(
        snapshot_path=snapshot_path,
        stage_definitions=stage_definitions,
        worker_done_event=worker_done_event,
    )
    window.setCentralWidget(board)
    window.showMaximized()

    def _finish() -> None:
        QTimer.singleShot(_AUTO_CLOSE_DELAY_MS, window.close)

    board.board_completed.connect(_finish)

    app.exec()

    if created_app:
        app.deleteLater()
        QtWidgets.QApplication.processEvents()
