"""Reuse only the exact idle target DOM after a positively unsent deferral."""

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.sync_api import ElementHandle, Locator, Page

OVERLAYS = ('[data-testid="confirmationSheetConfirm"]:visible, '
            '[data-testid="unretweetConfirm"]:visible, [data-testid="Dropdown"]:visible, [role="dialog"]:visible')


def _identity(url: object) -> tuple[str, str] | None:
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"x.com", "www.x.com"}:
        return None
    match = re.fullmatch(r"/([A-Za-z0-9_]+)/status/([0-9]+)(?:/(?:photo|video)/[0-9]+)?/?", parsed.path)
    return (match.group(1).casefold(), match.group(2)) if match else None


@dataclass(frozen=True)
class PendingView:
    page: Page
    target_url: str
    article: ElementHandle

    @classmethod
    def capture(cls, page: Page, target_url: str, locator: Locator) -> "PendingView | None":
        try:
            identity = _identity(target_url)
            if identity is None or _identity(page.url) != identity:
                return None
            if locator.count() != 1 or locator.is_visible() is not True or page.locator(OVERLAYS).count() != 0:
                return None
            article = locator.element_handle()
            return cls(page, target_url, article) if article is not None else None
        except Exception:
            return None

    def validate(self, page: Page, target_url: str, locator: Locator) -> bool:
        try:
            if page is not self.page or page.is_closed() or _identity(target_url) != _identity(self.target_url):
                return False
            if _identity(page.url) != _identity(self.target_url) or locator.count() != 1 or locator.is_visible() is not True:
                return False
            if page.locator(OVERLAYS).count() != 0:
                return False
            return page.evaluate("refs => refs.old === refs.current && refs.old.isConnected",
                                 {"old": self.article, "current": locator.element_handle()}) is True
        except Exception:
            return False
