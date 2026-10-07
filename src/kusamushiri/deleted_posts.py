"""IDs of posts this app deleted, kept per profile so archive imports can skip them."""

import contextlib
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from kusamushiri.logger import logger
from kusamushiri.paths import get_account_profile_dir

STORE_FILE_NAME = "deleted-posts.json"
STORE_VERSION = 1


class DeletedPostStore:
    """Deleted post IDs in a small JSON file.

    IDs are stored instead of URLs because archive URLs may use the /i/status/ fallback.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def for_account(cls, account_name: str | None) -> "DeletedPostStore":
        return cls(get_account_profile_dir(account_name) / STORE_FILE_NAME)

    def load(self) -> set[str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, ValueError) as error:
            logger.warning("Ignoring unreadable deleted-post file %s: %s", self.path, error)
            return set()
        ids = data.get("post_ids") if isinstance(data, dict) else None
        if not isinstance(ids, list):
            return set()
        return {post_id for post_id in ids if isinstance(post_id, str) and post_id}

    def record(self, post_ids: Iterable[str]) -> set[str]:
        """Merge the IDs into the file and return every stored ID."""
        stored = self.load()
        new_ids = {post_id for post_id in post_ids if post_id} - stored
        if not new_ids:
            return stored
        stored |= new_ids
        self._write(stored)
        return stored

    def _write(self, post_ids: set[str]) -> None:
        payload = {"version": STORE_VERSION, "post_ids": sorted(post_ids, key=lambda value: (len(value), value))}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write a sibling file and swap it in, so a crash never leaves a truncated store.
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(payload, file, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise
