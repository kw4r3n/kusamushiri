"""Add explicit recipe defaults without reserializing the user's TOML.

tomllib validates the complete document. The small scanner below only locates
insertion points; strings, comments and existing statements are never rewritten.
"""

import difflib
import math
import os
import stat
import tempfile
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kusamushiri.recipe import (
    DELETE_KEYS,
    CollectOptions,
    DeleteOptions,
    Recipe,
    RecipeError,
    find_recipes,
    format_recipe,
    parse_recipe,
)

_ARTIFACT_COMPONENTS = frozenset(
    {
        "toml",
        "bak",
        "backup",
        "tmp",
        "temp",
        "new",
        "updated",
        "generated",
        "preview",
        "candidate",
        "review",
    }
)


def discover_update_recipes(directory: Path) -> list[Path]:
    """Use the wizard's nonrecursive kusamushiri*.toml discovery, excluding artifacts."""
    return [
        path
        for path in find_recipes(directory)
        if not path.is_symlink()
        and not any(
            component.split("-", 1)[0].split("_", 1)[0] in _ARTIFACT_COMPONENTS
            for component in path.name[:-5].casefold().split(".")[1:]
        )
    ]


@dataclass(frozen=True)
class RecipeUpdate:
    path: Path
    original: bytes
    updated: bytes
    added_keys: tuple[str, ...]
    unknown_keys: tuple[str, ...]
    device: int
    inode: int
    mode: int

    @property
    def diff(self) -> str:
        lines = difflib.unified_diff(
            self.original.decode("utf-8").splitlines(keepends=True),
            self.updated.decode("utf-8").splitlines(keepends=True),
            fromfile=str(self.path),
            tofile=f"{self.path} (updated)",
        )
        return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


def _syntax_chars(text: str) -> Iterator[tuple[int, str]]:
    """Yield syntax outside strings/comments, including each string's opening quote."""
    index = 0
    while index < len(text):
        char = text[index]
        if char == "#":
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        yield index, char
        if char in "\"'":
            delimiter = char * (3 if text.startswith(char * 3, index) else 1)
            index += len(delimiter)
            while index < len(text):
                if char == '"' and text[index] == "\\":
                    index += 2
                elif text.startswith(delimiter, index):
                    index += len(delimiter)
                    # Multiline strings may end in four or five quotes.
                    if len(delimiter) == 3:
                        while index < len(text) and text[index] == char:
                            index += 1
                    break
                else:
                    index += 1
        else:
            index += 1


def _statements(text: str) -> Iterator[tuple[int, int]]:
    start: int | None = None
    depth = 0
    for index, char in _syntax_chars(text):
        if start is None and not char.isspace():
            start = index
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "\n" and depth == 0 and start is not None:
            yield start, index + 1
            start = None
    if start is not None:
        yield start, len(text)


def _header_name(statement: str) -> str | None:
    probe = "__kusamushiri_update_probe__"
    data = tomllib.loads(f"{statement}\n{probe} = true\n")
    return next((name for name in ("collect", "delete") if data == {name: {probe: True}}), None)


def _inline_table(statement: str) -> tuple[str, int, int] | None:
    syntax = list(_syntax_chars(statement))
    equals = next((index for index, char in syntax if char == "="), None)
    if equals is None:
        return None
    key = tomllib.loads(f"{statement[:equals]} = {{}}")
    name = next((name for name in ("collect", "delete") if key == {name: {}}), None)
    if name is None:
        return None
    value = [(index, char) for index, char in syntax if index > equals and not char.isspace()]
    if not value or value[0][1] != "{":
        return None
    depth = 0
    for index, char in value:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return name, value[0][0], index
    raise RecipeError("cannot locate inline table end")


def _known_recipe(data: Mapping[str, object], path: Path) -> tuple[Recipe, tuple[str, ...]]:
    """Validate supported settings, preserving but reporting unsupported keys."""
    allowed = {
        "": {"profile", "headless", "output_dir", "archive", "collect", "delete"},
        "collect": {*CollectOptions.__dataclass_fields__, "order"},
        "delete": DELETE_KEYS,
    }
    known = {key: value for key, value in data.items() if key in allowed[""]}
    unknown = [key for key in data if key not in allowed[""]]
    for name in ("collect", "delete"):
        table = data.get(name)
        if isinstance(table, dict):
            unknown.extend(f"{name}.{key}" for key in table if key not in allowed[name])
            known[name] = {key: value for key, value in table.items() if key in allowed[name]}
    return parse_recipe(known, path), tuple(sorted(unknown))


def _contains_original(before: object, after: object) -> bool:
    if isinstance(before, dict) and isinstance(after, dict):
        return all(key in after and _contains_original(value, after[key]) for key, value in before.items())
    if isinstance(before, list) and isinstance(after, list):
        return len(before) == len(after) and all(_contains_original(a, b) for a, b in zip(before, after, strict=True))
    if isinstance(before, float) and isinstance(after, float) and math.isnan(before) and math.isnan(after):
        return True
    return type(before) is type(after) and before == after


def _validate_update(original: bytes, updated: bytes, path: Path) -> tuple[str, ...]:
    before = tomllib.loads(original.decode("utf-8"))
    after = tomllib.loads(updated.decode("utf-8"))
    old_recipe, unknown = _known_recipe(before, path)
    new_recipe, new_unknown = _known_recipe(after, path)
    if not _contains_original(before, after) or old_recipe != new_recipe or unknown != new_unknown:
        raise RecipeError("update would change an existing setting or effective behavior")
    return unknown


def plan_recipe_update(path: Path) -> RecipeUpdate:
    """Read only; never add [delete] or invent a value for an unset optional field."""
    try:
        path = path.absolute()
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise RecipeError("recipe must be a regular file (symlinks are not updated)")
        original = path.read_bytes()
        text = original.decode("utf-8")
        data = tomllib.loads(text)
        _known_recipe(data, path)
        defaults = tomllib.loads(format_recipe(Recipe(path, delete=DeleteOptions())))
        missing: dict[str, dict[str, object]] = {
            "": {key: value for key, value in defaults.items() if key not in {"collect", "delete"} and key not in data},
        }
        for name in ("collect", "delete"):
            if name == "delete" and name not in data:
                continue
            existing = data.get(name, {})
            assert isinstance(existing, dict)  # validated above
            missing[name] = {key: value for key, value in defaults[name].items() if key not in existing}
            if name == "collect" and "oldest_first" in existing:
                missing[name].pop("order", None)

        # Use the canonical formatter's literals/notes, but keep every original byte.
        canonical = format_recipe(Recipe(path, delete=DeleteOptions()))
        lines: dict[str, dict[str, str]] = {"": {}, "collect": {}, "delete": {}}
        section = ""
        for line in canonical.splitlines():
            if line in ("[collect]", "[delete]"):
                section = line[1:-1]
            elif line and not line.startswith("#") and " = " in line:
                lines[section][line.split(" = ", 1)[0]] = line

        headers: dict[str, tuple[int, int]] = {}
        inline: dict[str, tuple[int, int]] = {}
        first_header = len(text)
        active: str | None = None
        for start, end in _statements(text):
            statement = text[start:end]
            if statement.startswith("["):
                first_header = min(first_header, start)
                if active is not None:
                    headers[active] = (headers[active][0], start)
                active = _header_name(statement)
                if active is not None:
                    headers[active] = (start, len(text))
            elif first_header == len(text):
                found = _inline_table(statement)
                if found is not None:
                    name, opening, closing = found
                    inline[name] = (start + opening, start + closing)

        newline = "\r\n" if "\r\n" in text else "\n"
        insertions: dict[int, str] = {}
        added: list[str] = []

        def insert(position: int, addition: str) -> None:
            insertions[position] = insertions.get(position, "") + addition

        def block(position: int, additions: list[str]) -> None:
            prefix = newline if position and text[position - 1] != "\n" else ""
            insert(position, prefix + newline.join(additions) + newline)

        new_sections: list[str] = []
        for name, values in missing.items():
            if not values:
                continue
            added.extend(f"{name}.{key}" if name else key for key in values)
            if name in inline:
                opening, closing = inline[name]
                prefix = ", " if text[opening + 1 : closing].strip() else ""
                literals = [lines[name][key].split("  #", 1)[0] for key in values]
                insert(closing, prefix + ", ".join(literals))
            elif name in headers:
                block(headers[name][1], [lines[name][key] for key in values])
            elif name and name not in data:
                new_sections.extend(["", f"[{name}]", *(lines[name][key] for key in values)])
            else:
                # Root and dotted-key tables stay before the first table header.
                prefix = f"{name}." if name else ""
                block(first_header, [prefix + lines[name][key] for key in values])

        if new_sections:
            block(len(text), new_sections)

        for position in sorted(insertions, reverse=True):
            text = text[:position] + insertions[position] + text[position:]
        updated = text.encode("utf-8")
        unknown = _validate_update(original, updated, path)
        return RecipeUpdate(
            path, original, updated, tuple(added), unknown, info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise RecipeError(f"{path}: {error}") from error


def _assert_unchanged(update: RecipeUpdate) -> None:
    info = update.path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or (info.st_dev, info.st_ino) != (update.device, update.inode)
        or stat.S_IMODE(info.st_mode) != update.mode
        or update.path.read_bytes() != update.original
    ):
        raise RecipeError("recipe changed since preview; rerun update-recipe")


def apply_recipe_update(update: RecipeUpdate) -> Path | None:
    """Validate, stage, back up exclusively, then atomically replace the recipe."""
    temporary: Path | None = None
    try:
        _assert_unchanged(update)
        _validate_update(update.original, update.updated, update.path)
        if update.updated == update.original:
            return None
        with tempfile.NamedTemporaryFile(
            dir=update.path.parent, prefix=f".{update.path.name}.", suffix=".tmp", delete=False
        ) as file:
            temporary = Path(file.name)
            os.chmod(temporary, update.mode)
            file.write(update.updated)
            file.flush()
            os.fsync(file.fileno())
        _validate_update(update.original, temporary.read_bytes(), update.path)
        _assert_unchanged(update)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = update.path.with_name(f"{update.path.name}.bak-{stamp}")
        # O_EXCL prevents clobbering another backup, and mode applies at creation.
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, update.mode)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(update.original)
                file.flush()
                os.fsync(file.fileno())
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
        _assert_unchanged(update)
        os.replace(temporary, update.path)
        temporary = None
        return backup
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise RecipeError(f"{update.path}: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
