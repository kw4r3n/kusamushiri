import os
from collections.abc import Iterator

import pytest

# GUI tests must not need a display; set before pytest-qt creates the QApplication.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def restore_playwright_browsers_path() -> Iterator[None]:
    # configure_frozen_browser_path() writes os.environ directly; a leaked value
    # makes every later Chromium test skip.
    saved = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    yield
    if saved is None:
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    else:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = saved
