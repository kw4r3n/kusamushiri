"""Bulk-delete X posts with a Qt desktop UI and Playwright automation."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kusamushiri")
except PackageNotFoundError:  # running from a source tree or frozen build without metadata
    __version__ = "0.0.0"
