import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# GUI tests must not need a display; set before pytest-qt creates the QApplication.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# The init-script test shells out to `node`, which developers usually get from
# a mise shim. Under the XDG isolation CONTRIBUTING recommends, the shim loses
# its trust store and refuses to start; trust the developer's own global mise
# config for this test process so the suite runs the same either way.
_mise_config = Path.home() / ".config" / "mise" / "config.toml"
if _mise_config.is_file():
    os.environ.setdefault("MISE_TRUSTED_CONFIG_PATHS", str(_mise_config))


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
