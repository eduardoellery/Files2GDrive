"""Exclusion rules for paths relative to a monitored Linux directory.

These rules intentionally do not implement shell expansion, negation, character
classes or gitignore semantics. A matching exclusion always wins. Combine global
and per-directory rules by passing both lists to :class:`FilterSet`.
"""

from collections.abc import Iterable
from dataclasses import dataclass
import re


def _validate_path(value: str) -> None:
    if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("Expected a non-empty relative path without '.' or '..' components")
    if "\x00" in value:
        raise ValueError("Paths cannot contain NUL")


def _translate(pattern: str) -> str:
    """Translate only the three supported wildcards to a regular expression."""
    pieces: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            pieces.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("**", index):
            pieces.append(".*")
            index += 2
        elif pattern[index] == "*":
            pieces.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            pieces.append("[^/]")
            index += 1
        else:
            pieces.append(re.escape(pattern[index]))
            index += 1
    return "".join(pieces)


@dataclass(frozen=True)
class _Rule:
    expression: re.Pattern[str]
    by_name: bool
    directory_only: bool

    def matches(self, path: str, is_dir: bool) -> bool:
        if self.directory_only and not is_dir:
            return False
        candidate = path.rsplit("/", 1)[-1] if self.by_name else path
        return self.expression.fullmatch(candidate) is not None


class FilterSet:
    """A union of exclusion patterns, with optional case-insensitive matching.

    ``*`` and ``?`` never cross a slash; ``**`` does. Patterns without a slash
    match item names at any depth. Other patterns match a path relative to the
    root. A trailing slash restricts a rule to directories and their descendants.
    Patterns and candidate paths must use Linux slash separators and be relative.
    Empty patterns are ignored; invalid relative paths raise ``ValueError``.
    """

    def __init__(self, patterns: Iterable[str], ignore_case: bool = False):
        self.patterns = tuple(pattern for pattern in patterns if pattern != "")
        self.ignore_case = ignore_case
        flags = re.DOTALL | (re.IGNORECASE if ignore_case else 0)
        rules = []
        for pattern in self.patterns:
            directory_only = pattern.endswith("/")
            body = pattern[:-1] if directory_only else pattern
            _validate_path(body)
            rules.append(_Rule(re.compile(_translate(body), flags), "/" not in pattern, directory_only))
        self._rules = tuple(rules)

    def excludes(self, relative_path: str, is_dir: bool = False) -> bool:
        """Return whether the item or any of its parent directories is excluded.

        No filesystem access is performed. ``is_dir`` describes the item itself;
        each ancestor is necessarily evaluated as a directory.
        """
        _validate_path(relative_path)
        components = relative_path.split("/")
        for end in range(1, len(components) + 1):
            path = "/".join(components[:end])
            candidate_is_dir = end < len(components) or is_dir
            if any(rule.matches(path, candidate_is_dir) for rule in self._rules):
                return True
        return False
