from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from files2gdrive.watcher import DirectoryWatcher, _DirtyHandler


class WatcherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_missing_watchdog_falls_back_with_initial_dirty_signal(self):
        with patch("files2gdrive.watcher.Observer", None):
            with DirectoryWatcher([self.root]) as watcher:
                self.assertEqual(watcher.backend, "polling")
                self.assertTrue(watcher.dirty.is_set())
                self.assertIn("watchdog unavailable", watcher.issues[0])

    def test_recursive_watch_and_shutdown_are_idempotent(self):
        observer = Mock()
        with patch("files2gdrive.watcher.Observer", return_value=observer):
            watcher = DirectoryWatcher([self.root])
            watcher.start()
            watcher.start()
            self.assertEqual(watcher.backend, "watchdog")
            observer.schedule.assert_called_once()
            self.assertEqual(observer.schedule.call_args.kwargs, {"recursive": True})
            observer.start.assert_called_once()
            watcher.stop()
            watcher.stop()
            observer.stop.assert_called_once()
            observer.join.assert_called_once()

    def test_unavailable_and_symlink_roots_are_skipped(self):
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        observer = Mock()
        with patch("files2gdrive.watcher.Observer", return_value=observer):
            with DirectoryWatcher([self.root / "missing", link]) as watcher:
                self.assertEqual(len(watcher.issues), 2)
                self.assertEqual(watcher.backend, "polling")
                observer.schedule.assert_not_called()
                observer.start.assert_not_called()

    def test_start_failure_falls_back(self):
        observer = Mock()
        observer.start.side_effect = OSError("watch limit exhausted")
        observer.is_alive.return_value = False
        with patch("files2gdrive.watcher.Observer", return_value=observer):
            with DirectoryWatcher([self.root]) as watcher:
                self.assertEqual(watcher.backend, "polling")
                self.assertIn("watch limit exhausted", watcher.issues[0])
                observer.stop.assert_called_once()

    def test_event_handler_ignores_reads(self):
        watcher = DirectoryWatcher([])
        handler = _DirtyHandler(watcher.dirty)
        for event_type in ("opened", "closed_no_write"):
            handler.on_any_event(SimpleNamespace(event_type=event_type))
            self.assertFalse(watcher.dirty.is_set())
        for event_type in ("created", "deleted", "modified", "moved", "closed"):
            handler.on_any_event(SimpleNamespace(event_type=event_type))
            self.assertTrue(watcher.dirty.is_set())
            watcher.dirty.clear()


if __name__ == "__main__":
    unittest.main()
