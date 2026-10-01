from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QPushButton, QTableWidget, QTableWidgetItem

from kusamushiri.i18n import tr
from kusamushiri.models import PostActionTarget, PostRecord

TEXT_COLUMN = 1
URL_COLUMN = 2
TEXT_COLUMN_MIN_WIDTH = 240
URL_COLUMN_WIDTH = 180


class PostTableManager(QObject):
    """Manages post table display and selection in XDeleterWindow."""

    def __init__(
        self,
        table: QTableWidget,
        select_all_button: QPushButton | None = None,
        on_status_update: Callable[[str], None] | None = None,
        on_busy: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__()
        self.table = table
        self._select_all_button = select_all_button
        self._on_status_update = on_status_update
        self._on_busy = on_busy
        table.viewport().installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Resize:
            self.fit_text_column()
        return False

    def fit_text_column(self) -> None:
        # Give the post text the remaining width, but never less than a readable
        # minimum; narrower windows scroll horizontally instead of hiding the text.
        other_width = sum(
            self.table.columnWidth(col)
            for col in range(self.table.columnCount())
            if col != TEXT_COLUMN
        )
        available = self.table.viewport().width() - other_width
        self.table.setColumnWidth(TEXT_COLUMN, max(TEXT_COLUMN_MIN_WIDTH, available))

    def _resize_columns(self) -> None:
        for col in range(self.table.columnCount()):
            if col not in (TEXT_COLUMN, URL_COLUMN):
                self.table.resizeColumnToContents(col)
        # Ensure checkbox column (0) isn't too narrow
        if self.table.columnWidth(0) < 40:
            self.table.setColumnWidth(0, 40)
        # Ensure certain columns have minimum widths
        min_widths = {3: 90, 4: 70, 5: 60, 6: 60, 7: 60, 8: 60}
        for col, width in min_widths.items():
            current = self.table.columnWidth(col)
            if current < width:
                self.table.setColumnWidth(col, width)
        # Full URLs stay in the tooltip and preview; the text is what users check.
        if self.table.columnWidth(URL_COLUMN) <= 0:
            self.table.setColumnWidth(URL_COLUMN, URL_COLUMN_WIDTH)
        self.fit_text_column()

    def display_posts(self, posts: list[PostRecord]) -> None:
        """Display posts in the table."""
        self.clear()

        for row_index, post in enumerate(posts):
            self.table.insertRow(row_index)

            check_item = QTableWidgetItem()
            check_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            check_item.setCheckState(Qt.CheckState.Checked)
            check_item.setData(
                Qt.ItemDataRole.UserRole,
                PostActionTarget(url=post.url, is_repost=post.is_repost),
            )
            self.table.setItem(row_index, 0, check_item)

            text_item = QTableWidgetItem(post.text.replace("\n", " "))
            text_item.setToolTip(post.text)
            self.table.setItem(row_index, 1, text_item)

            url_item = QTableWidgetItem(post.url)
            url_item.setToolTip(post.url)
            self.table.setItem(row_index, 2, url_item)

            try:
                dt = datetime.fromisoformat(post.date.replace("Z", "+00:00"))
                formatted = dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                formatted = post.date[:10]
            date_item = QTableWidgetItem(formatted)
            self.table.setItem(row_index, 3, date_item)

            kind_item = QTableWidgetItem(tr("リポスト") if post.is_repost else tr("ポスト"))
            kind_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row_index, 4, kind_item)

            likes_item = QTableWidgetItem()
            likes_item.setData(Qt.ItemDataRole.DisplayRole, post.likes)
            likes_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row_index, 5, likes_item)

            replies_item = QTableWidgetItem()
            replies_item.setData(Qt.ItemDataRole.DisplayRole, post.replies)
            replies_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row_index, 6, replies_item)

            media_item = QTableWidgetItem(tr("あり") if post.has_media else "-")
            media_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row_index, 7, media_item)

            reply_item = QTableWidgetItem(tr("はい") if post.is_reply else "-")
            reply_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row_index, 8, reply_item)

        self._resize_columns()
        self.table.setSortingEnabled(True)

        if self._on_busy:
            self._on_busy(False)
        if self._on_status_update:
            self._on_status_update(tr("収集完了: {count} 件の対象を取得しました。", count=len(posts)))

    def clear(self) -> None:
        """Clear all rows from the table."""
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        if self._select_all_button is not None:
            self._select_all_button.setEnabled(False)
        if self.table.rowCount() > 0:
            self.table.setSortingEnabled(True)

    def selected_targets(self) -> list[PostActionTarget]:
        """Get list of checked PostActionTarget items."""
        targets: list[PostActionTarget] = []
        for row_index in range(self.table.rowCount()):
            item = self.table.item(row_index, 0)
            if item is None:
                continue
            if item.checkState() == Qt.CheckState.Checked:
                target = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(target, PostActionTarget):
                    targets.append(target)
        return targets

    def toggle_all(self) -> None:
        """Toggle all checkboxes between checked and unchecked."""
        if self.table.rowCount() == 0:
            return

        all_checked = True
        for row_index in range(self.table.rowCount()):
            item = self.table.item(row_index, 0)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                all_checked = False
                break

        new_state = Qt.CheckState.Unchecked if all_checked else Qt.CheckState.Checked
        for row_index in range(self.table.rowCount()):
            item = self.table.item(row_index, 0)
            if item is not None:
                item.setCheckState(new_state)
