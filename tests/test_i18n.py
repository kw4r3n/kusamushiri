from __future__ import annotations

import ast
import string
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from kusamushiri import i18n
from kusamushiri.gui import XDeleterWindow
from kusamushiri.i18n import ENGLISH, get_language, set_language, tr, translate
from kusamushiri.models import CollectRequest
from kusamushiri.settings import AccountSettingsManager

SOURCE_DIR = Path(i18n.__file__).parent


def _translated_texts() -> set[str]:
    texts: set[str] = set()
    for path in SOURCE_DIR.glob("*.py"):
        if path.name == "i18n.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"tr", "translate"}:
                argument = node.args[0]
                assert isinstance(argument, ast.Constant) and isinstance(argument.value, str), (
                    f"{path.name}:{node.lineno} must pass a string literal"
                )
                texts.add(argument.value)
    return texts


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


@pytest.fixture(autouse=True)
def restore_language() -> Iterator[None]:
    yield
    set_language(None)


@pytest.fixture
def qsettings(tmp_path: Path) -> QSettings:
    return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)


def test_every_ui_text_has_an_english_translation() -> None:
    texts = _translated_texts()
    assert sorted(texts - ENGLISH.keys()) == []
    assert sorted(ENGLISH.keys() - texts) == []


def test_english_translations_keep_placeholders() -> None:
    for source, english in ENGLISH.items():
        assert _placeholders(source) == _placeholders(english), source


def test_tr_defaults_to_japanese_and_formats_values() -> None:
    assert get_language() == "ja"
    assert tr("{count} 件失敗", count=2) == "2 件失敗"
    set_language("en")
    assert tr("{count} 件失敗", count=2) == "2 failed"
    assert translate("待機中...", "ja") == "待機中..."


def test_unknown_language_falls_back_to_japanese() -> None:
    set_language("fr")
    assert get_language() == "ja"


def test_language_setting_round_trips(qsettings: QSettings) -> None:
    manager = AccountSettingsManager(qsettings)
    assert manager.get_language() == "ja"
    manager.save_language("en")
    assert AccountSettingsManager(qsettings).get_language() == "en"
    qsettings.setValue("language", "xx")
    assert manager.get_language() == "ja"


def test_validation_errors_follow_the_language() -> None:
    set_language("en")
    with pytest.raises(ValueError, match="Max posts must be 1 or more."):
        CollectRequest(
            username="alice",
            max_posts=0,
            media_filter="all",
            is_reply=False,
            min_likes=0,
            min_replies=0,
            search_mode="profile",
        ).validate()


def _build_window(monkeypatch: pytest.MonkeyPatch, settings: QSettings) -> XDeleterWindow:
    monkeypatch.setattr("kusamushiri.settings.list_saved_accounts", lambda: [])
    monkeypatch.setattr("kusamushiri.gui.AccountSettingsManager", lambda: AccountSettingsManager(settings))
    return XDeleterWindow()


def _close(window: XDeleterWindow) -> None:
    window.hide()
    window.deleteLater()
    app = QApplication.instance()
    assert app is not None
    app.processEvents()


def test_window_builds_in_english(qtbot, monkeypatch: pytest.MonkeyPatch, qsettings: QSettings) -> None:
    set_language("en")
    window = _build_window(monkeypatch, qsettings)
    try:
        assert window.collect_button.text() == "Collect and preview posts"
        assert window.export_following_button.text() == "Export following list"
        assert window.status_label.text() == "Idle..."
        assert window.language_combo.currentData() == "en"
    finally:
        _close(window)


def test_changing_language_saves_it_and_notes_the_restart(
    qtbot, monkeypatch: pytest.MonkeyPatch, qsettings: QSettings
) -> None:
    window = _build_window(monkeypatch, qsettings)
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(QMessageBox, "information", lambda _parent, title, text: shown.append((title, text)))
    try:
        assert window.language_combo.currentData() == "ja"
        window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
        assert AccountSettingsManager(qsettings).get_language() == "en"
        assert shown == [("Language changed", "The new language is applied after you restart the application.")]
        # The running window keeps its language until restart.
        assert get_language() == "ja"
        assert window.collect_button.text() == "ポストを収集＆プレビュー"
    finally:
        _close(window)
