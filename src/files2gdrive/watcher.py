"""Optional event notifications; periodic reconciliation remains authoritative."""

import os
from pathlib import Path
import stat
import threading

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:
    FileSystemEventHandler = object
    Observer = None


class _DirtyHandler(FileSystemEventHandler):
    def __init__(self, dirty: threading.Event):
        super().__init__()
        self.dirty = dirty

    def on_any_event(self, event):
        # Reading files during scanning/upload must not cause an event feedback loop.
        if event.event_type in {"created", "deleted", "modified", "moved", "closed"}:
            self.dirty.set()


class DirectoryWatcher:
    """Signal changes from observer threads without accessing the database.

    When watchdog is unavailable or startup fails, ``backend`` is ``polling``.
    The caller must periodically scan in either mode, including unavailable roots
    that may reappear. Recreate/restart this watcher when configured roots change.
    """

    def __init__(self, roots: list[str | os.PathLike[str]]):
        self.roots = [Path(root).absolute() for root in roots]
        self.dirty = threading.Event()
        self.issues: list[str] = []
        self.backend = "polling"
        self._observer = None
        self._started = False

    def start(self):
        if self._started:
            return self
        self._started = True
        self.issues.clear()
        self.dirty.set()
        if Observer is None:
            self.issues.append("watchdog unavailable; using periodic reconciliation")
            return self
        try:
            observer = Observer()
        except (OSError, RuntimeError) as error:
            self.issues.append(f"watchdog initialization failed: {error}; using periodic reconciliation")
            return self
        handler = _DirtyHandler(self.dirty)
        scheduled = 0
        for root in self.roots:
            try:
                info = root.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise OSError("root is not a directory or is a symbolic link")
                if not info.st_mode & 0o444 or not info.st_mode & 0o111:
                    raise PermissionError("root is not readable/searchable")
                observer.schedule(handler, str(root), recursive=True)
                scheduled += 1
            except (OSError, RuntimeError) as error:
                self.issues.append(f"{root}: {error}")
        if not scheduled:
            return self
        try:
            observer.start()
        except (OSError, RuntimeError) as error:
            self.issues.append(f"watchdog startup failed: {error}; using periodic reconciliation")
            observer.stop()
            if observer.is_alive():
                observer.join()
            return self
        self._observer = observer
        self.backend = "watchdog"
        return self

    def stop(self):
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        self._started = False
        self.backend = "polling"

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()
