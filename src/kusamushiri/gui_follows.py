"""Review dialog for the collected following list: filter, check, save and unfollow."""

from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from kusamushiri.follows import DEFAULT_UNFOLLOW_INTERVAL_SECONDS, FollowRecord, UnfollowResult, write_follow_list
from kusamushiri.gui_table import save_selected_with_dialog
from kusamushiri.i18n import tr

USERNAME_COLUMN = 1
DISPLAY_NAME_COLUMN = 2
FOLLOWS_YOU_COLUMN = 3


def format_unfollow_failures(failures: list[UnfollowResult]) -> str:
    lines: list[str] = []
    for result in failures:
        lines.append(f"@{result.target.username} {result.target.profile_url}")
        lines.append(f"  {result.error_message or tr('詳細不明')}")
    return "\n".join(lines)


class FollowListDialog(QDialog):
    """Non-modal, so the main window's stop button stays reachable during a run."""

    unfollow_requested = Signal(list, float)  # checked FollowRecords, interval seconds
    stop_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("フォローを整理"))
        self.resize(760, 560)
        self.source_username = ""
        self._busy = False
        self._can_run = False

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.hide_mutual_checkbox = QCheckBox(tr("相互フォローを除外"))
        self.hide_mutual_checkbox.setToolTip(tr("フォローされているアカウントを一覧から隠し、選択から外します。"))
        self.hide_mutual_checkbox.setChecked(True)
        self.hide_mutual_checkbox.toggled.connect(self._apply_mutual_filter)
        self.select_all_button = QPushButton(tr("すべて選択/解除"))
        self.select_all_button.setObjectName("secondaryButton")
        self.select_all_button.clicked.connect(self.toggle_all)
        self.selection_label = QLabel()
        self.selection_label.setObjectName("selectionCount")

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", tr("ユーザー名"), tr("表示名"), tr("フォローされている")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(DISPLAY_NAME_COLUMN, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._update_selection_label)

        self.interval_input = QDoubleSpinBox()
        self.interval_input.setRange(1.0, 600.0)
        self.interval_input.setDecimals(1)
        self.interval_input.setSingleStep(1.0)
        self.interval_input.setValue(DEFAULT_UNFOLLOW_INTERVAL_SECONDS)
        self.interval_input.setSuffix(tr(" 秒"))
        self.interval_input.setToolTip(
            tr("各アカウントのフォロー解除の間に待機する秒数です。X の制限を避けるため長めにしています。")
        )
        self.save_button = QPushButton(tr("選択項目を保存"))
        self.save_button.setObjectName("secondaryButton")
        self.save_button.clicked.connect(self._save_selected)
        self.stop_button = QPushButton(tr("ブラウザ停止 / 処理中止"))
        self.stop_button.setObjectName("secondaryButton")
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.unfollow_button = QPushButton(tr("選択したフォローを解除"))
        self.unfollow_button.setObjectName("dangerButton")
        self.unfollow_button.clicked.connect(self._request_unfollow)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.select_all_button)
        toolbar.addWidget(self.hide_mutual_checkbox)
        toolbar.addStretch(1)
        toolbar.addWidget(self.selection_label)
        footer = QHBoxLayout()
        footer.addWidget(QLabel(tr("解除間隔")))
        footer.addWidget(self.interval_input)
        footer.addStretch(1)
        footer.addWidget(self.save_button)
        footer.addWidget(self.stop_button)
        footer.addWidget(self.unfollow_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary_label)
        layout.addLayout(toolbar)
        layout.addWidget(self.table, 1)
        layout.addLayout(footer)
        self.set_busy(False, can_run=False)

    def set_records(self, username: str, records: list[FollowRecord], limit_reached: bool) -> None:
        self.source_username = username
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for row, record in enumerate(records):
            self.table.insertRow(row)
            check_item = QTableWidgetItem()
            check_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
            )
            # Unfollowing cannot be undone, so nothing starts checked.
            check_item.setCheckState(Qt.CheckState.Unchecked)
            check_item.setData(Qt.ItemDataRole.UserRole, record)
            self.table.setItem(row, 0, check_item)
            name_item = QTableWidgetItem(f"@{record.username}")
            name_item.setToolTip(record.profile_url)
            self.table.setItem(row, USERNAME_COLUMN, name_item)
            self.table.setItem(row, DISPLAY_NAME_COLUMN, QTableWidgetItem(record.display_name))
            self.table.setItem(row, FOLLOWS_YOU_COLUMN, QTableWidgetItem(tr("はい") if record.follows_you else ""))
        self.table.setSortingEnabled(True)
        self.table.blockSignals(False)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(USERNAME_COLUMN)
        self.table.resizeColumnToContents(FOLLOWS_YOU_COLUMN)
        mutual = sum(1 for record in records if record.follows_you)
        summary = tr(
            "@{username} のフォロー {count} 件（うち相互フォロー {mutual} 件）",
            username=username,
            count=len(records),
            mutual=mutual,
        )
        if limit_reached:
            summary += "\n" + tr("取得の安全上限に到達したため、一覧は途中までです。")
        self.summary_label.setText(summary)
        self._apply_mutual_filter()

    def _record(self, row: int) -> FollowRecord | None:
        item = self.table.item(row, 0)
        record = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return record if isinstance(record, FollowRecord) else None

    def _apply_mutual_filter(self, *_args: object) -> None:
        hide = self.hide_mutual_checkbox.isChecked()
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            record = self._record(row)
            hidden = hide and record is not None and record.follows_you
            self.table.setRowHidden(row, hidden)
            item = self.table.item(row, 0)
            if hidden and item is not None:
                item.setCheckState(Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._update_selection_label()

    def _visible_rows(self) -> list[int]:
        return [row for row in range(self.table.rowCount()) if not self.table.isRowHidden(row)]

    def selected_records(self) -> list[FollowRecord]:
        records: list[FollowRecord] = []
        for row in self._visible_rows():
            item = self.table.item(row, 0)
            record = self._record(row)
            if item is not None and record is not None and item.checkState() == Qt.CheckState.Checked:
                records.append(record)
        return records

    def toggle_all(self) -> None:
        rows = self._visible_rows()
        all_checked = bool(rows) and len(self.selected_records()) == len(rows)
        state = Qt.CheckState.Unchecked if all_checked else Qt.CheckState.Checked
        self.table.blockSignals(True)
        for row in rows:
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(state)
        self.table.blockSignals(False)
        self._update_selection_label()

    def remove_usernames(self, usernames: set[str]) -> None:
        folded = {username.casefold() for username in usernames}
        # One repaint for the whole batch instead of one per removed row.
        self.table.setUpdatesEnabled(False)
        try:
            for row in range(self.table.rowCount() - 1, -1, -1):
                record = self._record(row)
                if record is not None and record.username.casefold() in folded:
                    self.table.removeRow(row)
        finally:
            self.table.setUpdatesEnabled(True)
        self._update_selection_label()

    def _update_selection_label(self, *_args: object) -> None:
        self.selection_label.setText(
            tr(
                "{count} 件 / {selected} 件選択",
                count=len(self._visible_rows()),
                selected=len(self.selected_records()),
            )
        )

    def set_busy(self, busy: bool, *, can_run: bool) -> None:
        self._busy = busy
        self._can_run = can_run
        idle = not busy
        has_rows = self.table.rowCount() > 0
        self.table.setEnabled(idle)
        self.hide_mutual_checkbox.setEnabled(idle)
        self.select_all_button.setEnabled(idle and has_rows)
        self.interval_input.setEnabled(idle)
        self.save_button.setEnabled(idle and has_rows)
        self.unfollow_button.setEnabled(idle and can_run and has_rows)
        self.stop_button.setEnabled(busy)

    def _request_unfollow(self) -> None:
        if self._busy or not self._can_run:
            return
        records = self.selected_records()
        if not records:
            QMessageBox.information(self, tr("情報"), tr("フォロー解除するアカウントが選択されていません。"))
            return
        self.unfollow_requested.emit(records, self.interval_input.value())

    def _save_selected(self) -> None:
        records = self.selected_records()
        default_path = Path.home() / f"following-{self.source_username or 'selected'}-{date.today():%Y%m%d}.csv"
        save_selected_with_dialog(self, default_path, len(records), lambda path: write_follow_list(path, records))
