"""Private JSON-lines operation logs with bounded local retention.

Callers must not supply credentials or file contents in messages or extra fields.
Only explicitly supported extra fields are serialized. One process should own a
log directory; the logging handler's lock serializes threads in that process.
"""

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import uuid


_NAME = re.compile(r"operations-(\d+)-[0-9a-f]{32}\.jsonl\Z")
_FIELDS = ("operation", "path", "root_id", "result", "reason")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _files(directory: Path) -> list[tuple[datetime, Path]]:
    result = []
    if not directory.exists():
        return result
    for path in directory.iterdir():
        match = _NAME.fullmatch(path.name)
        if match and path.is_file() and not path.is_symlink():
            try:
                created = datetime.fromtimestamp(int(match[1]) / 1_000_000, timezone.utc)
            except (ValueError, OverflowError, OSError):
                continue
            result.append((created, path))
    return sorted(result, key=lambda item: (item[0], item[1].name))


class JsonLogHandler(logging.Handler):
    """Rotate JSONL files and discard oldest files at either retention limit.

    Age is based on the first event in each file, rather than modification time.
    Dropping an entire expired file can discard some newer events, so retention
    is *at most* max_age_days. Pruning occurs on initialization and each emit.
    An individual event larger than the total size limit is dropped, counted in
    ``dropped_records``, rather than writing truncated/invalid JSON.
    """

    def __init__(self, directory: str | Path, *, max_bytes: int = 100 * 1024 * 1024,
                 max_age_days: float = 30, file_bytes: int = 1024 * 1024,
                 clock: Callable[[], datetime] | None = None):
        super().__init__()
        if max_bytes <= 0 or file_bytes <= 0 or max_age_days <= 0:
            raise ValueError("Log retention limits must be positive")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes = max_bytes
        self.max_age = timedelta(days=max_age_days)
        self.file_bytes = min(file_bytes, max_bytes)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._current: Path | None = None
        self._created: datetime | None = None
        self.dropped_records = 0
        self._prune(_utc(self.clock()))

    def _prune(self, now: datetime) -> None:
        files = _files(self.directory)
        total = sum(path.stat().st_size for _, path in files)
        for created, path in files:
            if created <= now - self.max_age or total > self.max_bytes:
                size = path.stat().st_size
                path.unlink()
                total -= size
                if path == self._current:
                    self._current = None
                    self._created = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            now = _utc(self.clock())
            self._prune(now)
            event = {"timestamp": now.isoformat().replace("+00:00", "Z"),
                     "level": record.levelname, "message": record.getMessage()}
            for field in _FIELDS:
                event[field] = getattr(record, field, None)
            data = (json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
            if len(data) > self.max_bytes:
                self.dropped_records += 1
                return
            if (self._current is None or not self._current.exists()
                    or self._created.date() != now.date()
                    or self._current.stat().st_size + len(data) > self.file_bytes):
                stamp = int(now.timestamp() * 1_000_000)
                self._current = self.directory / f"operations-{stamp}-{uuid.uuid4().hex}.jsonl"
                self._created = now
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            else:
                flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
            fd = os.open(self._current, flags, 0o600)
            with os.fdopen(fd, "ab") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
            self._prune(now)
        except Exception:
            self.handleError(record)


def configure_logging(directory: str | Path, **options) -> logging.Logger:
    """Configure the application logger without duplicating managed handlers."""
    logger = logging.getLogger("files2gdrive")
    for handler in logger.handlers[:]:
        if isinstance(handler, JsonLogHandler):
            logger.removeHandler(handler)
            handler.close()
    logger.addHandler(JsonLogHandler(directory, **options))
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def read_logs(directory: str | Path, level: str | None = None,
              since: datetime | None = None, root_id: str | None = None) -> Iterator[dict]:
    """Yield valid retained events oldest first, with exact-level filtering.

    Incomplete lines after a crash are skipped. Missing files during concurrent
    rotation are tolerated. This reads only files owned by the handler naming
    scheme and never follows symbolic links. ``since`` is inclusive and aware.
    """
    cutoff = _utc(since) if since is not None else None
    for _, path in _files(Path(directory)):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except (FileNotFoundError, OSError):
            continue
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        continue
                    timestamp = _utc(datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")))
                    if level is not None and event.get("level") != level.upper():
                        continue
                    if cutoff is not None and timestamp < cutoff:
                        continue
                    if root_id is not None and event.get("root_id") != root_id:
                        continue
                    yield event
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
