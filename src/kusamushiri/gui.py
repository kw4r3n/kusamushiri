from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedLayout,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from kusamushiri.archive import ArchiveError, filter_archive_posts, load_archive_posts
from kusamushiri.exporting import EXPORT_SUFFIXES, write_post_list
from kusamushiri.follows import (
    DEFAULT_UNFOLLOW_INTERVAL_SECONDS,
    ExportFollowingRequest,
    FollowRecord,
    UnfollowRequest,
    UnfollowResult,
)
from kusamushiri.gui_follows import FollowListDialog, format_unfollow_failures
from kusamushiri.gui_table import URL_COLUMN_WIDTH, PostTableManager
from kusamushiri.i18n import LANGUAGE_NAMES, get_language, tr, translate
from kusamushiri.logger import logger
from kusamushiri.models import (
    DEFAULT_ACTION_INTERVAL_SECONDS,
    CollectRequest,
    ExecuteActionsRequest,
    PostActionResult,
    PostActionTarget,
    PostRecord,
    parse_keywords,
)
from kusamushiri.parsing import USERNAME_PATTERN
from kusamushiri.paths import DEFAULT_ACCOUNT_NAME, get_account_profile_dir, normalize_account_name
from kusamushiri.settings import AccountSettings, AccountSettingsManager

APP_STYLE_SHEET = """
QWidget {
    font-family: "Noto Sans CJK JP", "Yu Gothic", sans-serif;
    font-size: 13px;
    font-weight: 400;
    color: #e3e9f3;
}
QMainWindow, QWidget#central { background: #11151c; }
QWidget#settingsRail, QWidget#workspace { background: #191f29; border-radius: 8px; }
QLabel { background: transparent; }
QLabel#appTitle { font-size: 22px; font-weight: 600; }
QLabel#subtitle, QLabel#statusLabel, QLabel#selectionCount { color: #a2afc2; }
QLabel#emptyTitle { font-size: 18px; color: #d2dbea; }
QLabel#emptyHint { color: #a2afc2; }
QGroupBox {
    font-size: 16px; font-weight: 600; border: none;
    border-top: 1px solid #303a49; margin-top: 18px; padding-top: 16px;
}
QGroupBox::title { subcontrol-origin: margin; left: 0px; padding-right: 10px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QTextEdit {
    background: #151b24; border: 1px solid #303a49;
    border-radius: 8px; padding: 5px 9px; selection-background-color: #455ba5;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QDateEdit:focus, QTextEdit:focus { border: 1px solid #8296ff; }
QComboBox QAbstractItemView {
    background: #191f29; color: #e3e9f3; selection-background-color: #455ba5;
    border: 1px solid #303a49; padding: 4px;
}
QWidget:disabled { color: #718096; }
QPushButton {
    min-height: 36px; padding: 0 12px; border-radius: 8px;
    border: 1px solid #8296ff; background: #8296ff; color: #11151c; font-weight: 600;
}
QPushButton:hover { background: #9cacff; border-color: #9cacff; }
QPushButton:pressed { background: #6b80e8; }
QPushButton:focus { border: 2px solid #d0d9ff; }
QPushButton#secondaryButton {
    background: #222b38; border: 1px solid #3b4759; color: #d1daea; font-weight: 400;
}
QPushButton#secondaryButton:hover { background: #303c50; }
QPushButton#dangerButton { background: #b85b70; border-color: #b85b70; color: #ffffff; }
QPushButton#dangerButton:hover { background: #ce6b80; }
QPushButton#quietDangerButton {
    background: transparent; border: 1px solid #6a3845; color: #eba3b2; font-weight: 400;
}
QPushButton#quietDangerButton:hover { background: #2e1d24; border-color: #b85b70; }
QPushButton#secondaryButton:focus, QPushButton#dangerButton:focus,
QPushButton#quietDangerButton:focus { border: 2px solid #d0d9ff; }
QPushButton:disabled, QPushButton#secondaryButton:disabled, QPushButton#dangerButton:disabled,
QPushButton#quietDangerButton:disabled {
    background: #242c39; border-color: #303a49; color: #718096;
}
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 18px; height: 18px; }
QTableWidget {
    background: #151b24; alternate-background-color: #1a222e;
    border: 1px solid #303a49; border-radius: 8px;
    selection-background-color: #30436d; selection-color: #ffffff;
}
QHeaderView::section {
    background: #222b38; color: #bfcbdc; border: none;
    border-bottom: 1px solid #303a49; padding: 12px 8px; font-weight: 500;
}
QTableCornerButton::section { background: #222b38; border: none; }
QLabel#filterSummary { color: #a2afc2; padding: 4px 0; font-size: 12px; }
QProgressBar#progressBar {
    background: #151b24; border: 1px solid #303a49; border-radius: 5px;
    text-align: center; min-height: 18px; max-height: 18px;
}
QProgressBar#progressBar::chunk { background: #455ba5; border-radius: 4px; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: #191f29; width: 8px; margin: 0; }
QScrollBar::handle:vertical { background: #435065; border-radius: 4px; min-height: 32px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QSplitter::handle { background: #303a49; height: 3px; }
"""


def format_action_failures(failures: list[PostActionResult]) -> str:
    lines: list[str] = []
    for result in failures:
        error_message = result.error_message or tr("詳細不明")
        lines.append(f"[{result.action_label}] {result.target.url}")
        lines.append(f"  {error_message}")
    return "\n".join(lines)


def remove_successful_rows(table: QTableWidget, results: list[PostActionResult]) -> None:
    succeeded = {
        (result.target.url, result.target.kind)
        for result in results
        if result.success
    }
    if not succeeded:
        return

    for row_index in range(table.rowCount() - 1, -1, -1):
        item = table.item(row_index, 0)
        if item is None:
            continue
        target = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(target, PostActionTarget):
            continue
        if (target.url, target.kind) in succeeded:
            table.removeRow(row_index)


class XDeleterWindow(QMainWindow):
    start_browser_requested = Signal(str)  # account_name を渡す
    stop_browser_requested = Signal()
    check_login_requested = Signal()
    collect_requested = Signal(object)
    delete_requested = Signal(object)
    export_following_requested = Signal(object)
    collect_following_requested = Signal(str)
    unfollow_requested = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._busy = False
        self._browser_running = False
        self._login_verified = False
        self._stop_pending = False
        self._requested_action_count = 0
        self._requested_unfollow_count = 0
        self.follow_dialog: FollowListDialog | None = None
        self._last_logged_in_username: str | None = None
        self._current_account: str | None = None
        self._last_collect_request: CollectRequest | None = None
        self._settings_manager = AccountSettingsManager()
        self._build_ui()
        self._setup_shortcuts()
        self._setup_auto_save_timer()
        self._restore_settings()
        self.setStyleSheet(APP_STYLE_SHEET)
        self.set_busy(False)
        self.update_status(tr("待機中..."))

    def _setup_shortcuts(self) -> None:
        self._shortcut_delete = QShortcut(QKeySequence("Ctrl+Return"), self)
        self._shortcut_delete.activated.connect(self._handle_delete)

        self._shortcut_refresh = QShortcut(QKeySequence("Ctrl+R"), self)
        self._shortcut_refresh.activated.connect(self._start_search)

        shortcut_help = QShortcut(QKeySequence("Ctrl+H"), self)
        shortcut_help.activated.connect(self._show_shortcut_help)

    def _setup_auto_save_timer(self) -> None:
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.timeout.connect(self._auto_save_settings)
        self.auto_save_interval_input.valueChanged.connect(self._update_auto_save_timer)
        self._update_auto_save_timer(self.auto_save_interval_input.value())

    def _update_auto_save_timer(self, interval_seconds: int) -> None:
        self._auto_save_timer.setInterval(interval_seconds * 1_000)
        self._auto_save_timer.start()

    def _auto_save_settings(self) -> None:
        self._save_settings()
        logger.debug("Settings saved automatically for account: %s", self._current_account)

    def _build_ui(self) -> None:
        self.setWindowTitle("Kusamushiri")
        self.resize(1440, 960)
        self.setMinimumSize(980, 640)

        central_widget = QWidget(self)
        central_widget.setObjectName("central")
        root_layout = QVBoxLayout(central_widget)
        root_layout.setContentsMargins(20, 20, 20, 20)
        root_layout.setSpacing(14)
        self.setCentralWidget(central_widget)

        title = QLabel("Kusamushiri")
        title.setObjectName("appTitle")
        root_layout.addWidget(title)
        subtitle = QLabel(tr("ポストを確認して、必要なものだけ整理。"))
        subtitle.setObjectName("subtitle")
        root_layout.addWidget(subtitle)

        body = QHBoxLayout()
        body.setSpacing(16)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFixedWidth(350)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rail = QWidget()
        rail.setObjectName("settingsRail")
        settings_layout = QVBoxLayout(rail)
        settings_layout.setContentsMargins(16, 12, 16, 16)
        settings_layout.setSpacing(14)
        settings_layout.addWidget(self._build_account_group())
        settings_layout.addWidget(self._build_filter_group())
        settings_layout.addStretch()
        self.settings_scroll.setWidget(rail)
        body.addWidget(self.settings_scroll)

        workspace = QWidget()
        workspace.setObjectName("workspace")
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(16, 12, 16, 16)
        self.filter_summary = QLabel("")
        self.filter_summary.setObjectName("filterSummary")
        self.filter_summary.setWordWrap(True)
        self.filter_summary.hide()
        workspace_layout.addWidget(self.filter_summary)
        workspace_layout.addWidget(self._build_preview_group(), 1)
        workspace_layout.addWidget(self._build_log_group())
        body.addWidget(workspace, 1)
        root_layout.addLayout(body, 1)
        root_layout.addWidget(self._build_progress_bar())

        footer_layout = QHBoxLayout()
        footer_layout.setSpacing(10)

        self.select_all_button = QPushButton(tr("すべて選択/解除"))
        self.select_all_button.setObjectName("secondaryButton")
        self.select_all_button.clicked.connect(self._handle_toggle_all)

        self.status_label = QLabel(tr("待機中..."))
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumWidth(0)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        self.help_button = QPushButton("?")
        self.help_button.setObjectName("secondaryButton")
        self.help_button.setFixedWidth(38)
        self.help_button.setToolTip(tr("ショートカットキー一覧 (Ctrl+H)"))
        self.help_button.clicked.connect(self._show_shortcut_help)

        self.language_combo = QComboBox()
        self.language_combo.setToolTip(tr("言語 / Language"))
        for code, display_name in LANGUAGE_NAMES.items():
            self.language_combo.addItem(display_name, userData=code)
        self.language_combo.setCurrentIndex(self.language_combo.findData(get_language()))
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)

        self.stop_button = QPushButton(tr("ブラウザ停止 / 処理中止"))
        self.stop_button.setObjectName("secondaryButton")
        self.stop_button.clicked.connect(self._on_stop_browser_clicked)

        self.delete_button = QPushButton(tr("選択項目を削除/解除"))
        self.delete_button.setObjectName("dangerButton")
        self.delete_button.clicked.connect(self._handle_delete)

        self.preview_toolbar.insertWidget(0, self.select_all_button)
        footer_layout.addWidget(self.status_label, 1)
        footer_layout.addWidget(self.language_combo)
        footer_layout.addWidget(self.help_button)
        footer_layout.addWidget(self.stop_button)
        footer_layout.addWidget(self.delete_button)
        root_layout.addLayout(footer_layout)
        self.table_manager = PostTableManager(
            table=self.table,
            select_all_button=self.select_all_button,
            on_status_update=self.update_status,
            on_busy=self.set_busy,
        )

    def _build_account_group(self) -> QGroupBox:
        group = QGroupBox(tr("1. 基本設定"))
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(6)

        self.account_combo = QComboBox()
        self.account_combo.setEditable(True)
        self.account_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.account_combo.activated.connect(self._on_account_index_activated)
        account_editor = self.account_combo.lineEdit()
        if account_editor is not None:
            account_editor.editingFinished.connect(self._commit_typed_account)

        self.add_profile_button = QPushButton(tr("プロファイル追加"))
        self.add_profile_button.setObjectName("secondaryButton")
        self.add_profile_button.setToolTip(tr("新しいログイン用プロファイルを作成して選択します。"))
        self.add_profile_button.clicked.connect(self._add_profile)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("your_account")

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(tr("プロフィール走査"), userData="profile")
        self.mode_combo.addItem(tr("高度な検索"), userData="search")

        self.max_posts_input = QSpinBox()
        self.max_posts_input.setRange(1, 1000)
        self.max_posts_input.setValue(50)

        self.auto_save_interval_input = QSpinBox()
        self.auto_save_interval_input.setRange(10, 3600)
        self.auto_save_interval_input.setValue(60)
        self.auto_save_interval_input.setToolTip(tr("設定を自動保存する間隔です。"))

        self.start_button = QPushButton(tr("ブラウザ起動＆ログイン"))
        self.start_button.clicked.connect(self._handle_start_browser)
        self.rename_profile_button = QPushButton(tr("名前変更"))
        self.rename_profile_button.clicked.connect(self._rename_profile)
        self.delete_profile_button = QPushButton(tr("プロファイル削除"))
        self.delete_profile_button.clicked.connect(self._delete_profile)
        self.login_account_label = QLabel(tr("ログイン未確認"))
        self.export_following_button = QPushButton(tr("フォローリストをエクスポート"))
        self.export_following_button.setObjectName("secondaryButton")
        self.export_following_button.setToolTip(
            tr("X アカウントIDのフォロー一覧を CSV（または JSON）で保存します。")
        )
        self.export_following_button.clicked.connect(self._handle_export_following)
        self.manage_following_button = QPushButton(tr("フォローを整理"))
        self.manage_following_button.setObjectName("secondaryButton")
        self.manage_following_button.setToolTip(
            tr("フォロー一覧を取得して確認し、選んだアカウントだけフォローを解除します。")
        )
        self.manage_following_button.clicked.connect(self._handle_manage_following)

        layout.addWidget(QLabel(tr("プロファイル選択")), 0, 0, 1, 2)
        layout.addWidget(self.account_combo, 1, 0, 1, 2)
        profile_actions = QHBoxLayout()
        profile_actions.setSpacing(6)
        for button in (self.add_profile_button, self.rename_profile_button, self.delete_profile_button):
            button.setObjectName("secondaryButton")
            button.setStyleSheet("font-size: 11px; padding: 0 5px;")
            profile_actions.addWidget(button)
        # Deleting a login profile is irreversible; keep it visibly apart from add/rename.
        self.delete_profile_button.setObjectName("quietDangerButton")
        layout.addLayout(profile_actions, 2, 0, 1, 2)
        layout.addWidget(QLabel(tr("X アカウントID")), 3, 0, 1, 2)
        layout.addWidget(self.username_input, 4, 0, 1, 2)
        layout.addWidget(QLabel(tr("収集モード")), 5, 0, 1, 2)
        layout.addWidget(self.mode_combo, 6, 0, 1, 2)
        layout.addWidget(QLabel(tr("取得上限件数")), 7, 0)
        layout.addWidget(QLabel(tr("自動保存間隔(秒)")), 7, 1)
        layout.addWidget(self.max_posts_input, 8, 0)
        layout.addWidget(self.auto_save_interval_input, 8, 1)
        layout.addWidget(self.login_account_label, 9, 0, 1, 2)
        layout.addWidget(self.start_button, 10, 0, 1, 2)
        layout.addWidget(self.export_following_button, 11, 0, 1, 2)
        layout.addWidget(self.manage_following_button, 12, 0, 1, 2)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        return group

    def _build_filter_group(self) -> QGroupBox:
        group = QGroupBox(tr("2. 絞り込み条件"))
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(6)

        self.media_filter_combo = QComboBox()
        self.media_filter_combo.addItem(tr("すべて"), userData="all")
        self.media_filter_combo.addItem(tr("画像・動画ありのみ"), userData="with_media")
        self.media_filter_combo.addItem(tr("画像・動画なしのみ"), userData="without_media")

        self.post_kind_combo = QComboBox()
        self.post_kind_combo.addItem(tr("通常ポストのみ"), userData="posts")
        self.post_kind_combo.addItem(tr("リポストのみ"), userData="reposts")
        self.post_kind_combo.addItem(tr("通常ポスト + リポスト"), userData="all")
        self.post_kind_combo.addItem(tr("いいねしたポスト（いいね取り消し）"), userData="likes")
        self.post_kind_combo.setItemData(
            3,
            tr("プロフィールのいいね欄を走査し、チェックしたポストのいいねを取り消します。"),
            Qt.ItemDataRole.ToolTipRole,
        )
        self.post_kind_combo.currentIndexChanged.connect(self._update_mode_availability)

        self.reply_only_checkbox = QCheckBox(tr("リプライのみ"))

        self.min_likes_input = QSpinBox()
        self.min_likes_input.setRange(0, 1_000_000)

        self.min_replies_input = QSpinBox()
        self.min_replies_input.setRange(0, 1_000_000)

        self.since_date_checkbox = QCheckBox(tr("この日以降"))
        self.since_date_checkbox.toggled.connect(self._toggle_since_date)

        self.since_date_input = QDateEdit(QDate.currentDate())
        self.since_date_input.setCalendarPopup(True)
        self.since_date_input.setDisplayFormat("yyyy-MM-dd")
        self.since_date_input.setEnabled(False)
        self.since_date_input.setToolTip(tr("指定した日付を含む、それ以降のポストを対象にします。"))

        self.until_date_checkbox = QCheckBox(tr("この日以前"))
        self.until_date_checkbox.toggled.connect(self._toggle_until_date)

        self.until_date_input = QDateEdit(QDate.currentDate())
        self.until_date_input.setCalendarPopup(True)
        self.until_date_input.setDisplayFormat("yyyy-MM-dd")
        self.until_date_input.setEnabled(False)
        self.until_date_input.setToolTip(tr("指定した日付を含む、それ以前のポストを対象にします。"))

        self.include_keywords_input = QLineEdit()
        self.include_keywords_input.setPlaceholderText(tr("例: 懸賞, キャンペーン"))
        self.include_keywords_input.setToolTip(
            tr("いずれかのキーワードを本文に含むポストだけを対象にします。カンマ区切りで複数指定できます。")
        )

        self.exclude_keywords_input = QLineEdit()
        self.exclude_keywords_input.setPlaceholderText(tr("例: 固定, 大事"))
        self.exclude_keywords_input.setToolTip(
            tr("いずれかのキーワードを本文に含むポストを対象から外します。カンマ区切りで複数指定できます。")
        )

        self.collect_button = QPushButton(tr("ポストを収集＆プレビュー"))
        self.collect_button.clicked.connect(self._handle_collect)
        self.import_archive_button = QPushButton(tr("X のアーカイブから読み込み"))
        self.import_archive_button.setObjectName("secondaryButton")
        self.import_archive_button.setToolTip(
            tr("X の「データのアーカイブ」(zip) からポストを読み込み、上の条件で絞り込みます。検索で見つからない古いポストも対象にできます。")
        )
        self.import_archive_button.clicked.connect(self._handle_import_archive)

        layout.addWidget(QLabel(tr("メディア条件")), 0, 0, 1, 2)
        layout.addWidget(self.media_filter_combo, 1, 0, 1, 2)
        layout.addWidget(QLabel(tr("対象種別")), 2, 0, 1, 2)
        layout.addWidget(self.post_kind_combo, 3, 0, 1, 2)
        layout.addWidget(self.reply_only_checkbox, 4, 0, 1, 2)
        layout.addWidget(QLabel(tr("最低いいね数")), 5, 0)
        layout.addWidget(QLabel(tr("最低返信数")), 5, 1)
        layout.addWidget(self.min_likes_input, 6, 0)
        layout.addWidget(self.min_replies_input, 6, 1)
        layout.addWidget(self.since_date_checkbox, 7, 0)
        layout.addWidget(self.since_date_input, 7, 1)
        layout.addWidget(self.until_date_checkbox, 8, 0)
        layout.addWidget(self.until_date_input, 8, 1)
        layout.addWidget(QLabel(tr("含むキーワード")), 9, 0, 1, 2)
        layout.addWidget(self.include_keywords_input, 10, 0, 1, 2)
        layout.addWidget(QLabel(tr("除外キーワード")), 11, 0, 1, 2)
        layout.addWidget(self.exclude_keywords_input, 12, 0, 1, 2)
        layout.addWidget(self.collect_button, 13, 0, 1, 2)
        layout.addWidget(self.import_archive_button, 14, 0, 1, 2)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        return group

    def _build_preview_group(self) -> QGroupBox:
        group = QGroupBox(tr("3. プレビューと削除/解除実行"))
        layout = QVBoxLayout(group)

        layout.setContentsMargins(0, 12, 0, 0)
        self.selection_count = QLabel(tr("0 件 / 0 件選択"))
        self.selection_count.setObjectName("selectionCount")
        layout.addWidget(self.selection_count)
        action_options_layout = QHBoxLayout()
        self.preview_toolbar = action_options_layout
        action_options_layout.addWidget(QLabel(tr("削除/解除間隔")))

        self.delete_interval_input = QDoubleSpinBox()
        self.delete_interval_input.setRange(0.0, 60.0)
        self.delete_interval_input.setDecimals(1)
        self.delete_interval_input.setSingleStep(0.5)
        self.delete_interval_input.setValue(DEFAULT_ACTION_INTERVAL_SECONDS)
        self.delete_interval_input.setSuffix(tr(" 秒"))
        self.delete_interval_input.setToolTip(tr("各項目の削除/解除の間に待機する秒数です。"))
        action_options_layout.addWidget(self.delete_interval_input)

        interval_note = QLabel(tr("0 秒で連続実行"))
        action_options_layout.addWidget(interval_note)
        action_options_layout.addStretch(1)
        self.export_posts_button = QPushButton(tr("選択項目を保存"))
        self.export_posts_button.setObjectName("secondaryButton")
        self.export_posts_button.setToolTip(tr("チェックした項目の本文や URL を CSV（または JSON）で保存します。削除前の控えに使えます。"))
        self.export_posts_button.clicked.connect(self._handle_export_posts)
        action_options_layout.addWidget(self.export_posts_button)
        layout.addLayout(action_options_layout)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            tr("削除"),
            tr("本文"),
            "URL",
            tr("日時"),
            tr("種別"),
            tr("いいね"),
            tr("返信"),
            tr("メディア"),
            tr("リプライ"),
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(False)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)
        # Text width is fitted by PostTableManager so it keeps a readable minimum.
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(2, URL_COLUMN_WIDTH)
        for column in range(3, 9):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

        table_container = QWidget()
        table_stack = QStackedLayout(table_container)
        table_stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        table_stack.setContentsMargins(0, 0, 0, 0)
        table_stack.addWidget(self.table)
        self.empty_state = QWidget()
        self.empty_state.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setContentsMargins(16, 56, 16, 16)
        empty_layout.addStretch()
        self.empty_title = QLabel(tr("まだポストがありません"))
        self.empty_title.setObjectName("emptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint = QLabel(tr("ログイン後、条件を指定してポストを収集してください。"))
        self.empty_hint.setObjectName("emptyHint")
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_hint)
        empty_layout.addStretch()
        table_stack.addWidget(self.empty_state)
        table_stack.setCurrentWidget(self.empty_state)
        layout.addWidget(table_container, 1)
        # Coalesce model notifications while collecting hundreds of rows.
        self._selection_timer = QTimer(self)
        self._selection_timer.setSingleShot(True)
        self._selection_timer.timeout.connect(self._update_selection_summary)
        self.table.itemChanged.connect(self._schedule_selection_summary)
        self.table.model().rowsInserted.connect(self._schedule_selection_summary)
        self.table.model().rowsRemoved.connect(self._schedule_selection_summary)
        self.table.model().modelReset.connect(self._schedule_selection_summary)

        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setFixedHeight(100)
        self.preview_text.setPlaceholderText(tr("行を選択すると本文とURLが表示されます。"))
        layout.addWidget(self.preview_text)
        self.table.itemSelectionChanged.connect(self._on_post_selected)

        return group

    def _build_log_group(self) -> QGroupBox:
        group = QGroupBox(tr("ログ"))
        group.setCheckable(True)
        group.setChecked(False)
        layout = QVBoxLayout(group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFixedHeight(120)
        self.log_text.hide()
        self.log_text.setPlaceholderText(tr("ステータスメッセージがここに記録されます。"))
        layout.addWidget(self.log_text)
        group.toggled.connect(self._toggle_log_visibility)
        return group

    def _schedule_selection_summary(self, *_args: object) -> None:
        self._selection_timer.start(0)

    def _update_selection_summary(self) -> None:
        count = self.table.rowCount()
        selected = sum(
            1 for row in range(count)
            if (item := self.table.item(row, 0)) is not None
            and item.checkState() == Qt.CheckState.Checked
        )
        self.selection_count.setText(tr("{count} 件 / {selected} 件選択", count=count, selected=selected))
        self.empty_state.setVisible(count == 0)

    def _set_empty_copy(self, title: str, hint: str) -> None:
        self.empty_title.setText(title)
        self.empty_hint.setText(hint)

    def _toggle_log_visibility(self, checked: bool) -> None:
        self.log_text.setVisible(checked)

    def _build_progress_bar(self) -> QProgressBar:
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("progressBar")
        self.progress_bar.setVisible(False)
        self.progress_bar.setValue(0)
        return self.progress_bar

    def show_progress(self, current: int, total: int) -> None:
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(current)
        self.progress_bar.setVisible(True)

    def hide_progress(self) -> None:
        self.progress_bar.setVisible(False)
        self.progress_bar.setValue(0)

    def set_busy(self, busy: bool) -> None:
        # A cancelled job still reports completion before the stop is acknowledged;
        # keep controls locked until on_browser_stopped so nothing queues behind it.
        busy = busy or self._stop_pending
        self._busy = busy
        enabled = not busy
        self._shortcut_delete.setEnabled(enabled)
        self._shortcut_refresh.setEnabled(enabled)
        self.account_combo.setEnabled(enabled)
        self.add_profile_button.setEnabled(enabled)
        can_manage = enabled and not self._browser_running and self._current_account != DEFAULT_ACCOUNT_NAME
        self.rename_profile_button.setEnabled(can_manage)
        self.delete_profile_button.setEnabled(can_manage)
        self.login_account_label.setText(
            tr("ログイン中: @{username}", username=self._last_logged_in_username)
            if self._login_verified and self._last_logged_in_username else tr("ログイン未確認")
        )
        self.start_button.setEnabled(enabled)
        self.collect_button.setEnabled(enabled and self._login_verified)
        self.import_archive_button.setEnabled(enabled)
        self.export_following_button.setEnabled(enabled and self._login_verified)
        self.manage_following_button.setEnabled(enabled and self._login_verified)
        if self.follow_dialog is not None:
            self.follow_dialog.set_busy(busy, can_run=self._login_verified)
        self.delete_button.setEnabled(enabled and self._login_verified and self.table.rowCount() > 0)
        self.delete_interval_input.setEnabled(enabled)
        self.table.setEnabled(enabled)
        self.select_all_button.setEnabled(enabled and self.table.rowCount() > 0)
        self.export_posts_button.setEnabled(enabled and self.table.rowCount() > 0)
        self.stop_button.setEnabled(self._browser_running or busy)
        if not busy:
            self.hide_progress()

    def update_status(self, text: str) -> None:
        logger.debug("Status update: %s", text)
        self.status_label.setText(text)
        self.log_text.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")

    def _show_shortcut_help(self) -> None:
        QMessageBox.information(
            self,
            tr("ショートカットキー一覧"),
            tr(
                "Ctrl+Enter : 選択項目を削除/解除\n"
                "Ctrl+R    : 検索/収集を実行\n"
                "Ctrl+H    : このヘルプを表示"
            ),
        )

    def _on_language_changed(self, _index: int) -> None:
        language = self.language_combo.currentData()
        if not isinstance(language, str):
            return
        self._settings_manager.save_language(language)
        # Widgets keep the current language until restart, so this notice uses the chosen one.
        QMessageBox.information(
            self,
            translate("言語を変更しました", language),
            translate("表示言語はアプリケーションの再起動後に切り替わります。", language),
        )

    def _on_post_selected(self) -> None:
        rows = self.table.selectedItems()
        if not rows:
            self.preview_text.clear()
            return
        selected_rows: set[int] = set()
        for item in rows:
            selected_rows.add(item.row())
        texts: list[str] = []
        for row in sorted(selected_rows):
            text_item = self.table.item(row, 1)
            url_item = self.table.item(row, 2)
            if text_item:
                texts.append(f"{text_item.text()}\n{url_item.text() if url_item else ''}")
        self.preview_text.setText("\n---\n".join(texts) if texts else "")

    def _update_filter_summary(self, request: CollectRequest | None = None) -> None:
        if request is None:
            self.filter_summary.setText("")
            self.filter_summary.hide()
            return
        parts: list[str] = []
        media_map = {"all": tr("すべて"), "with_media": tr("画像/動画あり"), "without_media": tr("画像/動画なし")}
        kind_map = {"posts": tr("ポストのみ"), "reposts": tr("リポストのみ"), "all": tr("すべて"), "likes": tr("いいね")}
        parts.append(tr("メディア: {value}", value=media_map.get(request.media_filter, request.media_filter)))
        parts.append(tr("種別: {value}", value=kind_map.get(request.post_kind_filter, request.post_kind_filter)))
        parts.append(tr("最大: {count}件", count=request.max_posts))
        if request.min_likes > 0:
            parts.append(tr("いいね≥{count}", count=request.min_likes))
        if request.min_replies > 0:
            parts.append(tr("返信≥{count}", count=request.min_replies))
        if request.since_date:
            parts.append(tr("{date}〜", date=request.since_date))
        if request.until_date:
            parts.append(tr("〜{date}", date=request.until_date))
        if request.include_keywords:
            parts.append(tr("含む: {keywords}", keywords=", ".join(request.include_keywords)))
        if request.exclude_keywords:
            parts.append(tr("除外: {keywords}", keywords=", ".join(request.exclude_keywords)))
        self.filter_summary.setText(" | ".join(parts))
        self.filter_summary.show()

    def show_login_wait_dialog(self) -> None:
        self._browser_running = True
        self.set_busy(False)
        QMessageBox.information(
            self,
            tr("ログイン待ち"),
            tr(
                "ブラウザが開きました。\n"
                "X に手動でログインし、ホーム画面が表示されたら OK を押してください。"
            ),
        )
        self.check_login_requested.emit()

    def show_error(self, message: str) -> None:
        self._stop_pending = False
        self.set_busy(False)
        QMessageBox.critical(self, tr("エラー"), message)

    def _handle_start_browser(self) -> None:
        if self._busy:
            return
        logger.info("GUI: Start Browser button clicked.")
        account_name = self._commit_typed_account(load_settings=False)
        if self._busy:  # Account switching first needs an acknowledged browser stop.
            return
        self._browser_running = True  # Keep profile management locked even if startup partially fails.
        self._login_verified = False
        self.set_busy(True)
        self.update_status(tr("ブラウザを起動しています..."))
        self.start_browser_requested.emit(account_name)

    def _stop_browser(self) -> None:
        self._stop_pending = True
        self.set_busy(True)
        self.update_status(tr("処理を中断してブラウザを停止しています..."))
        self.stop_browser_requested.emit()

    def on_browser_stopped(self) -> None:
        self._stop_pending = False
        self._browser_running = False
        self._login_verified = False
        self._last_logged_in_username = None
        self.set_busy(False)

    def _on_stop_browser_clicked(self) -> None:
        reply = QMessageBox.question(
            self,
            tr("ブラウザを停止"),
            tr("ブラウザを停止しますか？\n進行中の操作は安全な区切りで中断されます。"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._stop_browser()

    def _ensure_account_option(self, account_name: str) -> None:
        if self.account_combo.findText(account_name) < 0:
            self.account_combo.addItem(account_name)

        index = self.account_combo.findText(account_name)
        if index >= 0 and self.account_combo.currentIndex() != index:
            self.account_combo.setCurrentIndex(index)
        elif self.account_combo.currentText() != account_name:
            self.account_combo.setCurrentText(account_name)

    def _change_current_account(self, account_name: str, *, load_settings: bool) -> str:
        """アカウント選択が変更された時の処理"""
        normalized_account_name = normalize_account_name(account_name)
        if self._busy:
            return self._current_account or normalized_account_name
        self._ensure_account_option(normalized_account_name)
        if normalized_account_name == self._current_account:
            return normalized_account_name

        # 前のアカウント設定を保存
        if self._current_account:
            self._save_settings()

        # 新しいアカウント設定を読み込み
        self._current_account = normalized_account_name
        if load_settings:
            self._load_account_settings(normalized_account_name)
        self._login_verified = False
        self._last_logged_in_username = None
        self._last_collect_request = None
        if self.follow_dialog is not None:
            # The follow list belongs to the previous account's session.
            self.follow_dialog.close()
            self.follow_dialog.set_records("", [], False)
        self._clear_posts()
        self.preview_text.clear()
        self._update_filter_summary()
        if self._browser_running:
            self._stop_browser()
        else:
            self.set_busy(False)
        self.update_status(tr("アカウント '{name}' を選択しました。", name=normalized_account_name))
        logger.info("Switched to account: %s", normalized_account_name)
        return normalized_account_name

    def _rename_profile(self) -> None:
        if self._busy or self._browser_running or self._current_account in (None, DEFAULT_ACCOUNT_NAME):
            return
        name, accepted = QInputDialog.getText(self, tr("名前変更"), tr("新しいプロファイル名:"))
        if not accepted or not name.strip().removeprefix("@").strip():
            return
        new_name = normalize_account_name(name)
        if self.account_combo.findText(new_name) >= 0:
            QMessageBox.warning(self, tr("名前変更"), tr("同じ名前のプロファイルが既にあります。"))
            return
        try:
            self._save_settings()
            self._settings_manager.rename_account(self._current_account, new_name)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, tr("名前変更"), str(error))
            return
        index = self.account_combo.findText(self._current_account)
        self.account_combo.setItemText(index, new_name)
        self._current_account = new_name
        self.update_status(tr("プロファイル名を '{name}' に変更しました。", name=new_name))

    def _delete_profile(self) -> None:
        if self._busy or self._browser_running or self._current_account in (None, DEFAULT_ACCOUNT_NAME):
            return
        name = self._current_account
        reply = QMessageBox.question(
            self, tr("プロファイル削除"), tr("'{name}' のログイン情報と設定を完全に削除しますか？\nXのアカウントやポストは削除されません。", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self._settings_manager.delete_account(name)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, tr("プロファイル削除"), str(error))
            return
        self.account_combo.removeItem(self.account_combo.findText(name))
        self._current_account = None  # Do not save the deleted account during switching.
        self._change_current_account(DEFAULT_ACCOUNT_NAME, load_settings=True)
        self._save_settings()
        self.update_status(tr("プロファイル '{name}' を削除しました。", name=name))

    def _add_profile(self) -> None:
        if self._busy:
            return
        name, accepted = QInputDialog.getText(self, tr("プロファイル追加"), tr("新しいプロファイル名:"))
        if not accepted or self._busy:
            return
        if not name.strip().removeprefix("@").strip():
            QMessageBox.warning(self, tr("プロファイル追加"), tr("プロファイル名を入力してください。"))
            return
        account_name = normalize_account_name(name)
        if self.account_combo.findText(account_name) >= 0 or account_name in self._settings_manager.list_accounts():
            QMessageBox.warning(self, tr("プロファイル追加"), tr("同じ名前のプロファイルが既にあります。"))
            return

        try:
            get_account_profile_dir(account_name)
        except OSError as error:
            logger.exception("Failed to create profile for %s", account_name)
            QMessageBox.warning(self, tr("プロファイル追加"), tr("プロファイルを作成できませんでした: {error}", error=error))
            return

        # Initialize every setting so legacy unprefixed settings cannot leak into a new profile.
        self._settings_manager.save_account_settings(
            account_name, AccountSettings(since_date="", until_date="")
        )
        self._change_current_account(account_name, load_settings=True)
        self._save_settings()
        self.update_status(tr("プロファイル '{name}' を追加しました。ブラウザを起動してログインしてください。", name=account_name))

    def _on_account_index_activated(self, index: int) -> None:
        self._change_current_account(self.account_combo.itemText(index), load_settings=True)

    def _commit_typed_account(self, load_settings: bool = False) -> str:
        return self._change_current_account(self.account_combo.currentText(), load_settings=load_settings)

    def _set_combo_current_data(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _restore_settings(self) -> None:
        # アカウント一覧の復元
        accounts = self._settings_manager.list_accounts()
        self.account_combo.clear()
        self.account_combo.addItem(DEFAULT_ACCOUNT_NAME)
        self.account_combo.addItems([account for account in accounts if account != DEFAULT_ACCOUNT_NAME])

        # 前回のアカウントを復元
        last_account = self._settings_manager.get_last_account()
        index = self.account_combo.findText(last_account)
        if index >= 0:
            self.account_combo.setCurrentIndex(index)
        else:
            self.account_combo.addItem(last_account)
            self.account_combo.setCurrentIndex(self.account_combo.findText(last_account))

        self._current_account = self.account_combo.currentText()
        self._load_account_settings(self._current_account)

    def _load_account_settings(self, account_name: str) -> None:
        """指定アカウントの設定を復元"""
        settings = self._settings_manager.load_account_settings(account_name)

        self.username_input.setText(settings.username)
        self._set_combo_current_data(self.mode_combo, settings.search_mode)
        self.max_posts_input.setValue(settings.max_posts)
        self._set_combo_current_data(self.media_filter_combo, settings.media_filter)
        self._set_combo_current_data(self.post_kind_combo, settings.post_kind_filter)
        self.reply_only_checkbox.setChecked(settings.reply_only)
        self.min_likes_input.setValue(settings.min_likes)
        self.min_replies_input.setValue(settings.min_replies)
        self.delete_interval_input.setValue(settings.action_interval_seconds)
        self.auto_save_interval_input.setValue(settings.auto_save_interval_seconds)
        self.include_keywords_input.setText(settings.include_keywords)
        self.exclude_keywords_input.setText(settings.exclude_keywords)

        today = QDate.currentDate().toString(Qt.DateFormat.ISODate)
        since_date = QDate.fromString(
            settings.since_date or today,
            Qt.DateFormat.ISODate,
        )
        until_date = QDate.fromString(
            settings.until_date or today,
            Qt.DateFormat.ISODate,
        )
        if since_date.isValid():
            self.since_date_input.setDate(since_date)
        if until_date.isValid():
            self.until_date_input.setDate(until_date)

        self.since_date_checkbox.setChecked(settings.since_date_enabled)
        self.until_date_checkbox.setChecked(settings.until_date_enabled)
        self._toggle_since_date(self.since_date_checkbox.isChecked())
        self._toggle_until_date(self.until_date_checkbox.isChecked())

    def _save_settings(self) -> None:
        """現在のアカウント設定を保存"""
        if not self._current_account:
            self._current_account = normalize_account_name(self.account_combo.currentText())

        self._settings_manager.save_account_settings(
            self._current_account,
            AccountSettings(
                username=self.username_input.text().strip(),
                search_mode=self.mode_combo.currentData(),
                max_posts=self.max_posts_input.value(),
                media_filter=self.media_filter_combo.currentData(),
                post_kind_filter=self.post_kind_combo.currentData(),
                reply_only=self.reply_only_checkbox.isChecked(),
                min_likes=self.min_likes_input.value(),
                min_replies=self.min_replies_input.value(),
                action_interval_seconds=self.delete_interval_input.value(),
                auto_save_interval_seconds=self.auto_save_interval_input.value(),
                since_date=self.since_date_input.date().toString(Qt.DateFormat.ISODate),
                until_date=self.until_date_input.date().toString(Qt.DateFormat.ISODate),
                since_date_enabled=self.since_date_checkbox.isChecked(),
                until_date_enabled=self.until_date_checkbox.isChecked(),
                include_keywords=self.include_keywords_input.text().strip(),
                exclude_keywords=self.exclude_keywords_input.text().strip(),
            ),
        )

    def _normalized_username_input(self) -> str:
        return self.username_input.text().strip().removeprefix("@")

    def _confirm_account_match(self, target_username: str | None = None) -> bool:
        if not self._last_logged_in_username:
            return True

        input_username = self._normalized_username_input() if target_username is None else target_username
        if not input_username:
            return True
        if input_username.casefold() == self._last_logged_in_username.casefold():
            return True

        result = QMessageBox.question(
            self,
            tr("アカウント不一致"),
            tr(
                "現在ログイン中のアカウントと入力された対象アカウントが一致していません。\n"
                "ログイン中: @{logged_in}\n"
                "入力値: @{entered}\n\n"
                "このまま実行すると削除/解除に失敗する可能性があります。続行しますか？",
                logged_in=self._last_logged_in_username,
                entered=input_username,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return result == QMessageBox.StandardButton.Yes

    def on_login_checked(self, is_logged_in: bool, current_username: str) -> None:
        if not is_logged_in:
            self._login_verified = False
            self._last_logged_in_username = None
            self.set_busy(False)
            return

        self._login_verified = True
        self._last_logged_in_username = current_username or None
        self.set_busy(False)
        if not current_username:
            return

        input_username = self._normalized_username_input()
        if not input_username:
            self.username_input.setText(current_username)
            self.update_status(tr("ログイン確認済み: @{username}", username=current_username))
            return

        if input_username.casefold() != current_username.casefold():
            logger.warning(
                "Logged-in username mismatch detected: logged_in=%s input=%s",
                current_username,
                input_username,
            )
            QMessageBox.warning(
                self,
                tr("アカウント不一致"),
                tr(
                    "現在ログイン中のアカウントと入力された対象アカウントが一致していません。\n"
                    "ログイン中: @{logged_in}\n"
                    "入力値: @{entered}\n\n"
                    "必要に応じて対象アカウントを修正してください。",
                    logged_in=current_username,
                    entered=input_username,
                ),
            )

    def _update_mode_availability(self) -> None:
        likes_selected = self.post_kind_combo.currentData() == "likes"
        self.mode_combo.setEnabled(not likes_selected)
        self.mode_combo.setToolTip(
            tr("いいねはプロフィールのいいね欄から収集するため、収集モードは使いません。") if likes_selected else ""
        )

    def _toggle_since_date(self, checked: bool) -> None:
        self.since_date_input.setEnabled(checked)

    def _toggle_until_date(self, checked: bool) -> None:
        self.until_date_input.setEnabled(checked)

    def _handle_collect(self) -> None:
        if self._busy:
            return
        logger.info("GUI: Collect Posts button clicked.")
        self._commit_typed_account(load_settings=False)

        if not self._login_verified:
            QMessageBox.information(self, tr("ログイン確認が必要"), tr("先にブラウザを起動してログインを確認してください。"))
            return

        request = self._build_collect_request()
        if request is None:
            return

        self._clear_posts()
        self.set_busy(True)
        self.show_progress(0, 0)
        self.update_status(tr("対象を収集しています..."))
        self._last_collect_request = request
        self._update_filter_summary(request)
        self._save_settings()
        self.collect_requested.emit(request)

    def _build_collect_request(self) -> CollectRequest | None:
        """Read the filter inputs; show a warning and return None when they are invalid."""
        if self.since_date_checkbox.isChecked() and self.until_date_checkbox.isChecked():
            since_qdate = self.since_date_input.date()
            until_qdate = self.until_date_input.date()
            since = date(since_qdate.year(), since_qdate.month(), since_qdate.day())
            until = date(until_qdate.year(), until_qdate.month(), until_qdate.day())
            if since > until:
                QMessageBox.warning(
                    self,
                    tr("日付範囲エラー"),
                    tr("開始日が終了日より後の日付になっています。\n正しい範囲を指定してください。"),
                )
                return None

        since_date: date | None = None
        if self.since_date_checkbox.isChecked():
            selected_date = self.since_date_input.date()
            since_date = date(selected_date.year(), selected_date.month(), selected_date.day())

        until_date: date | None = None
        if self.until_date_checkbox.isChecked():
            selected_date = self.until_date_input.date()
            until_date = date(selected_date.year(), selected_date.month(), selected_date.day())

        request = CollectRequest(
            username=self.username_input.text().strip(),
            max_posts=self.max_posts_input.value(),
            media_filter=self.media_filter_combo.currentData(),
            is_reply=self.reply_only_checkbox.isChecked(),
            min_likes=self.min_likes_input.value(),
            min_replies=self.min_replies_input.value(),
            # Likes are listed only on the profile's likes timeline, never via search.
            search_mode="profile" if self.post_kind_combo.currentData() == "likes" else self.mode_combo.currentData(),
            post_kind_filter=self.post_kind_combo.currentData(),
            since_date=since_date,
            until_date=until_date,
            include_keywords=parse_keywords(self.include_keywords_input.text()),
            exclude_keywords=parse_keywords(self.exclude_keywords_input.text()),
        )

        try:
            request.validate()
        except ValueError as error:
            logger.warning("Collect request validation failed: %s", error)
            QMessageBox.warning(self, tr("入力エラー"), str(error))
            return None
        return request

    def _handle_import_archive(self) -> None:
        if self._busy:
            return
        logger.info("GUI: Import archive button clicked.")
        self._commit_typed_account(load_settings=False)
        request = self._build_collect_request()
        if request is None:
            return
        path_text, _selected_filter = QFileDialog.getOpenFileName(
            self,
            tr("X のアーカイブを選択"),
            str(Path.home()),
            tr("X のアーカイブ (*.zip tweets.js tweets-part*.js tweet.js)"),
        )
        if not path_text:
            return
        archive_path = Path(path_text)
        # A picked tweets.js stands for the extracted archive folder around it.
        if archive_path.suffix.lower() == ".js":
            archive_path = archive_path.parent
        try:
            posts = load_archive_posts(archive_path, fallback_username=request.username)
        except ArchiveError as error:
            logger.warning("Archive import failed: %s", error)
            QMessageBox.warning(
                self, tr("読み込みエラー"), tr("アーカイブを読み込めませんでした: {error}", error=error)
            )
            return

        # Archive reposts carry the repost's own ID, not the original post's, so they cannot be undone by URL.
        # Archives have no reply counts either, so the minimum-replies filter is not applied.
        reposts = sum(1 for post in posts if post.kind == "repost")
        matched = filter_archive_posts(
            (post for post in posts if post.kind != "repost"), replace(request, min_replies=0)
        )[: request.max_posts]

        self._last_collect_request = request
        self._save_settings()
        self.display_posts(matched)
        self.update_status(
            tr(
                "アーカイブから {count} 件を読み込みました（全 {total} 件、リポスト {reposts} 件は対象外）。",
                count=len(matched),
                total=len(posts),
                reposts=reposts,
            )
        )

    def _start_search(self) -> None:
        if self._busy:
            return
        self._handle_collect()

    def display_posts(self, posts: list[PostRecord]) -> None:
        """Display posts in the table."""
        logger.info("GUI: Received %s posts to display.", len(posts))
        self.table_manager.display_posts(posts)
        if not posts:
            self._set_empty_copy(
                tr("条件に一致するポストはありませんでした"),
                tr("期間やフィルタを広げて、もう一度収集してください。"),
            )
        self.hide_progress()
        self._update_filter_summary(self._last_collect_request)

    def on_collection_progress(self, scanned: int, current: int, total: int) -> None:
        self.update_status(tr("対象を収集中: {scanned} 件走査済み ({current}/{total})", scanned=scanned, current=current, total=total))
        self.show_progress(current, total)

    def _handle_export_following(self) -> None:
        if self._busy:
            return
        self._commit_typed_account(load_settings=False)
        if not self._login_verified:
            QMessageBox.information(self, tr("ログイン確認が必要"), tr("先にブラウザを起動してログインを確認してください。"))
            return

        username = self._normalized_username_input()
        if not USERNAME_PATTERN.fullmatch(username):
            QMessageBox.warning(self, tr("入力エラー"), tr("X アカウントIDを入力してください（英数字とアンダースコア、15文字まで）。"))
            return
        default_path = Path.home() / f"following-{username}-{date.today():%Y%m%d}.csv"
        path_text, _selected_filter = QFileDialog.getSaveFileName(
            self,
            tr("フォローリストの保存先"),
            str(default_path),
            "CSV (*.csv);;JSON (*.json)",
        )
        if not path_text:
            return
        output_path = Path(path_text)
        if output_path.suffix.lower() not in {".csv", ".json"}:
            output_path = output_path.with_name(f"{output_path.name}.csv")

        request = ExportFollowingRequest(username=username, output_path=output_path)
        try:
            request.validate()
        except ValueError as error:
            QMessageBox.warning(self, tr("入力エラー"), str(error))
            return

        self.set_busy(True)
        self.show_progress(0, 0)
        self.update_status(tr("@{username} のフォローリストを取得しています...", username=username))
        self.export_following_requested.emit(request)

    def on_following_progress(self, count: int) -> None:
        self.update_status(tr("フォローリストを取得中: {count} 件", count=count))

    def on_following_export_finished(self, path: str, count: int) -> None:
        self.set_busy(False)
        if not path:
            return
        self.update_status(tr("フォローリスト {count} 件を保存しました: {path}", count=count, path=path))
        QMessageBox.information(self, tr("エクスポート完了"), tr("{count} 件のフォローを保存しました。\n{path}", count=count, path=path))

    def _handle_manage_following(self) -> None:
        if self._busy:
            return
        self._commit_typed_account(load_settings=False)
        if not self._login_verified:
            QMessageBox.information(self, tr("ログイン確認が必要"), tr("先にブラウザを起動してログインを確認してください。"))
            return
        username = self._normalized_username_input()
        if not USERNAME_PATTERN.fullmatch(username):
            QMessageBox.warning(self, tr("入力エラー"), tr("X アカウントIDを入力してください（英数字とアンダースコア、15文字まで）。"))
            return
        self.set_busy(True)
        self.show_progress(0, 0)
        self.update_status(tr("@{username} のフォローリストを取得しています...", username=username))
        self.collect_following_requested.emit(username)

    def _ensure_follow_dialog(self) -> FollowListDialog:
        if self.follow_dialog is None:
            self.follow_dialog = FollowListDialog(self)
            self.follow_dialog.unfollow_requested.connect(self._handle_unfollow)
            self.follow_dialog.stop_requested.connect(self._on_stop_browser_clicked)
        return self.follow_dialog

    def on_following_collected(self, username: str, records: list[FollowRecord], limit_reached: bool) -> None:
        stopped = self._stop_pending
        dialog = self._ensure_follow_dialog()
        dialog.set_records(username, records, limit_reached)
        self.set_busy(False)
        if stopped:
            return
        self.update_status(tr("フォロー {count} 件を取得しました。", count=len(records)))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _handle_unfollow(self, records: list[FollowRecord], interval_seconds: float) -> None:
        logger.info("GUI: Unfollow requested for %s accounts.", len(records))
        if self._busy:
            return
        if not self._login_verified:
            QMessageBox.information(self, tr("ログイン確認が必要"), tr("現在のアカウントでログインを確認してください。"))
            return
        source_username = self.follow_dialog.source_username if self.follow_dialog is not None else None
        if not self._confirm_account_match(source_username):
            logger.info("GUI: Unfollow cancelled due to account mismatch warning.")
            return
        request = UnfollowRequest(targets=list(records), interval_seconds=interval_seconds)
        try:
            request.validate()
        except ValueError as error:
            QMessageBox.warning(self, tr("入力エラー"), str(error))
            return
        result = QMessageBox.question(
            self.follow_dialog or self,
            tr("確認"),
            tr(
                "{count} 件のフォローを解除します。\n（この操作は元に戻せません）\n"
                "各アカウントの間に {interval} 秒待機します。続行しますか？",
                count=len(records),
                interval=f"{interval_seconds:g}",
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            logger.info("GUI: Unfollow cancelled by user.")
            return
        self._submit_unfollow(request, tr("フォロー解除を実行しています..."))

    def _submit_unfollow(self, request: UnfollowRequest, status: str) -> None:
        self._requested_unfollow_count = len(request.targets)
        self.set_busy(True)
        self.update_status(status)
        self.unfollow_requested.emit(request)

    def on_unfollow_progress(self, username: str, current: int, total: int) -> None:
        self.update_status(
            tr("フォロー解除中 ({current}/{total}): @{username}", current=current, total=total, username=username)
        )
        self.show_progress(current, total)

    def on_unfollow_done(self, results: list[UnfollowResult]) -> None:
        requested = max(self._requested_unfollow_count, len(results))
        succeeded = {result.target.username for result in results if result.success}
        failures = [result for result in results if not result.success]
        skipped = requested - len(results)
        if self.follow_dialog is not None:
            self.follow_dialog.remove_usernames(succeeded)
        stopped = self._stop_pending
        self.set_busy(False)
        heading = tr("フォロー解除を中止") if stopped else tr("フォロー解除完了")
        message = tr("{heading}: {success} / {requested} 件成功", heading=heading, success=len(succeeded), requested=requested)
        notes = [tr("{count} 件失敗", count=len(failures))] if failures else []
        if skipped > 0:
            notes.append(tr("{count} 件未処理", count=skipped))
        if notes:
            message += tr("（{notes}）", notes=tr("、").join(notes))
        self.update_status(message)
        if stopped:
            return
        parent = self.follow_dialog or self
        if not failures:
            QMessageBox.information(parent, tr("完了"), message)
            return

        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("一部失敗"))
        box.setText(message)
        box.setInformativeText(tr("失敗した項目の詳細を確認し、失敗分だけ再試行できます。"))
        box.setDetailedText(format_unfollow_failures(failures))
        retry_button = box.addButton(tr("失敗分だけ再試行"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()

        if box.clickedButton() == retry_button:
            self._retry_failed_unfollows(failures)

    def _retry_failed_unfollows(self, failures: list[UnfollowResult]) -> None:
        if self._busy or not self._login_verified:
            QMessageBox.information(self, tr("ログイン確認が必要"), tr("現在のアカウントでログインを確認してください。"))
            return
        source_username = self.follow_dialog.source_username if self.follow_dialog is not None else None
        if not self._confirm_account_match(source_username):
            logger.info("GUI: Unfollow retry cancelled due to account mismatch warning.")
            return
        logger.info("GUI: Retrying %s failed unfollows.", len(failures))
        interval_seconds = (
            self.follow_dialog.interval_input.value()
            if self.follow_dialog is not None
            else DEFAULT_UNFOLLOW_INTERVAL_SECONDS
        )
        self._submit_unfollow(
            UnfollowRequest(targets=[result.target for result in failures], interval_seconds=interval_seconds),
            tr("失敗分を再試行しています..."),
        )

    def _handle_export_posts(self) -> None:
        if self._busy:
            return
        posts = self.table_manager.selected_posts()
        if not posts:
            QMessageBox.information(self, tr("情報"), tr("保存する項目が選択されていません。"))
            return
        username = self._normalized_username_input() or "posts"
        default_path = Path.home() / f"posts-{username}-{date.today():%Y%m%d}.csv"
        path_text, _selected_filter = QFileDialog.getSaveFileName(
            self,
            tr("選択項目の保存先"),
            str(default_path),
            "CSV (*.csv);;JSON (*.json)",
        )
        if not path_text:
            return
        output_path = Path(path_text)
        if output_path.suffix.lower() not in EXPORT_SUFFIXES:
            output_path = output_path.with_name(f"{output_path.name}.csv")
        try:
            write_post_list(output_path, posts)
        except OSError as error:
            logger.warning("Post export failed: %s", error)
            QMessageBox.warning(self, tr("保存エラー"), tr("ファイルを保存できませんでした: {error}", error=error))
            return
        self.update_status(tr("選択項目 {count} 件を保存しました: {path}", count=len(posts), path=output_path))
        QMessageBox.information(
            self,
            tr("エクスポート完了"),
            tr("{count} 件の項目を保存しました。\n{path}", count=len(posts), path=output_path),
        )

    def _clear_posts(self) -> None:
        self.table_manager.clear()

    def _selected_targets(self) -> list[PostActionTarget]:
        return self.table_manager.selected_targets()

    def _handle_toggle_all(self) -> None:
        self.table_manager.toggle_all()

    def _handle_delete(self) -> None:
        logger.info("GUI: Delete button clicked.")
        if self._busy:
            return
        if not self._login_verified:
            QMessageBox.information(self, tr("ログイン確認が必要"), tr("現在のアカウントでログインを確認してください。"))
            return
        targets = self._selected_targets()
        if not targets:
            QMessageBox.information(self, tr("情報"), tr("実行対象が選択されていません。"))
            return
        if not self._confirm_account_match():
            logger.info("GUI: Execution cancelled due to account mismatch warning.")
            return

        delete_count = sum(1 for target in targets if target.kind == "post")
        unrepost_count = sum(1 for target in targets if target.kind == "repost")
        unlike_count = sum(1 for target in targets if target.kind == "like")
        operations: list[str] = []
        if delete_count > 0:
            operations.append(tr("ポスト削除 {count} 件", count=delete_count))
        if unrepost_count > 0:
            operations.append(tr("リポスト解除 {count} 件", count=unrepost_count))
        if unlike_count > 0:
            operations.append(tr("いいね取り消し {count} 件", count=unlike_count))

        summary = " / ".join(operations)

        result = QMessageBox.question(
            self,
            tr("確認"),
            tr(
                "選択された {count} 件の項目に対して、{summary} を実行しますか？\n（この操作は元に戻せません）",
                count=len(targets),
                summary=summary,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            logger.info("GUI: Deletion cancelled by user.")
            return

        request = ExecuteActionsRequest(
            targets=targets,
            interval_seconds=self.delete_interval_input.value(),
        )
        try:
            request.validate()
        except ValueError as error:
            logger.warning("Execute request validation failed: %s", error)
            QMessageBox.warning(self, tr("入力エラー"), str(error))
            return

        self._submit_delete(request, tr("削除/解除処理を実行しています..."))

    def _submit_delete(self, request: ExecuteActionsRequest, status: str) -> None:
        self._requested_action_count = len(request.targets)
        self.set_busy(True)
        self.update_status(status)
        self._save_settings()
        self.delete_requested.emit(request)

    def on_delete_progress(self, action_label: str, url: str, current: int, total: int) -> None:
        self.update_status(tr("{action}中 ({current}/{total}): {url}", action=action_label, current=current, total=total, url=url))
        self.show_progress(current, total)

    def on_delete_done(self, results: list[PostActionResult]) -> None:
        requested = max(self._requested_action_count, len(results))
        success_count = sum(1 for result in results if result.success)
        failures = [result for result in results if not result.success]
        skipped = requested - len(results)
        remove_successful_rows(self.table, results)
        if self.table.rowCount() == 0 and success_count > 0:
            self._set_empty_copy(
                tr("選択した項目をすべて処理しました"),
                tr("続けて整理する場合は、条件を指定してもう一度収集してください。"),
            )
        stopped = self._stop_pending
        self.set_busy(False)
        heading = tr("削除/解除処理を中止") if stopped else tr("削除/解除処理完了")
        message = tr("{heading}: {success} / {requested} 件成功", heading=heading, success=success_count, requested=requested)
        notes = [tr("{count} 件失敗", count=len(failures))] if failures else []
        if skipped > 0:
            notes.append(tr("{count} 件未処理", count=skipped))
        if notes:
            message += tr("（{notes}）", notes=tr("、").join(notes))
        self.update_status(message)
        if stopped:
            # The stop dialog already had the user's attention; report in the status line.
            return
        if not failures:
            QMessageBox.information(self, tr("完了"), message)
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("一部失敗"))
        box.setText(message)
        box.setInformativeText(tr("失敗した項目の詳細を確認し、失敗分だけ再試行できます。"))
        box.setDetailedText(format_action_failures(failures))
        retry_button = box.addButton(tr("失敗分だけ再試行"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()

        if box.clickedButton() == retry_button:
            self._retry_failed(failures)

    def _retry_failed(self, failures: list[PostActionResult]) -> None:
        # Same guards as the delete button: the target field may have changed
        # while the result dialog was open, or the browser may have stopped.
        if self._busy or not self._login_verified:
            QMessageBox.information(self, tr("ログイン確認が必要"), tr("現在のアカウントでログインを確認してください。"))
            return
        if not self._confirm_account_match():
            logger.info("GUI: Retry cancelled due to account mismatch warning.")
            return
        logger.info("GUI: Retrying %s failed items.", len(failures))
        retry_request = ExecuteActionsRequest(
            targets=[result.target for result in failures],
            interval_seconds=self.delete_interval_input.value(),
        )
        self._submit_delete(retry_request, tr("失敗分を再試行しています..."))

    def closeEvent(self, event: QCloseEvent) -> None:
        question = tr("アプリケーションを終了しますか？")
        if self._busy:
            question = tr("処理中ですがアプリケーションを終了しますか？")

        result = QMessageBox.question(
            self,
            tr("終了"),
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result == QMessageBox.StandardButton.Yes:
            self._save_settings()
            event.accept()
        else:
            event.ignore()
