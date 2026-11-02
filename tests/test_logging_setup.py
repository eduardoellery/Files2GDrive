from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import tempfile
import unittest

from files2gdrive.logging_setup import JsonLogHandler, configure_logging, read_logs


class LogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "logs"
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.logger = logging.Logger("test", level=logging.INFO)

    def tearDown(self):
        for handler in self.logger.handlers:
            handler.close()
        self.temp.cleanup()

    def handler(self, **kwargs):
        handler = JsonLogHandler(self.directory, clock=lambda: self.now, **kwargs)
        self.logger.addHandler(handler)
        return handler

    def test_json_fields_permissions_and_filters(self):
        self.handler()
        self.logger.info("uploaded %s", "file", extra={"operation": "upload", "path": ".config/a", "root_id": "r1", "result": "success", "unapproved": "not exported"})
        first = self.now
        self.now += timedelta(seconds=1)
        self.logger.warning("retry", extra={"root_id": "r2", "reason": "offline"})
        events = list(read_logs(self.directory))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["timestamp"], "2026-01-01T00:00:00Z")
        self.assertEqual(events[0]["path"], ".config/a")
        self.assertEqual(events[0]["operation"], "upload")
        self.assertNotIn("unapproved", events[0])
        self.assertEqual(len(list(read_logs(self.directory, root_id="r1"))), 1)
        self.assertEqual(len(list(read_logs(self.directory, level="warning"))), 1)
        self.assertEqual(len(list(read_logs(self.directory, since=first + timedelta(seconds=1)))), 1)
        for path in self.directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for line in path.read_text().splitlines():
                self.assertIsInstance(json.loads(line), dict)

    def test_total_size_is_bounded_and_oldest_events_are_removed(self):
        self.handler(max_bytes=1000, file_bytes=300)
        for number in range(30):
            self.now += timedelta(seconds=1)
            self.logger.info("event-%02d %s", number, "x" * 30)
        self.assertLessEqual(sum(p.stat().st_size for p in self.directory.iterdir()), 1000)
        events = list(read_logs(self.directory))
        self.assertGreater(len(events), 0)
        self.assertIn("event-29", events[-1]["message"])
        self.assertFalse(any("event-00" in event["message"] for event in events))

    def test_retention_prunes_expired_files_on_emit_and_startup(self):
        handler = self.handler()
        self.logger.info("old")
        self.now += timedelta(days=29)
        self.logger.info("recent")
        self.assertEqual(len(list(read_logs(self.directory))), 2)
        self.now += timedelta(days=1)
        self.logger.info("new")
        self.assertEqual([e["message"] for e in read_logs(self.directory)], ["recent", "new"])
        self.logger.removeHandler(handler)
        handler.close()
        self.now += timedelta(days=31)
        self.handler()
        self.assertEqual(list(read_logs(self.directory)), [])

    def test_oversized_record_is_dropped_without_broken_json(self):
        handler = self.handler(max_bytes=250)
        self.logger.info("x" * 1000)
        self.assertEqual(handler.dropped_records, 1)
        self.logger.info("small")
        self.assertEqual([e["message"] for e in read_logs(self.directory)], ["small"])

    def test_partial_record_and_symlink_are_ignored(self):
        self.handler()
        self.logger.info("complete")
        path = next(self.directory.iterdir())
        with path.open("a") as stream:
            stream.write('{"timestamp":')
        outside = Path(self.temp.name) / "outside"
        outside.write_text("not a log")
        (self.directory / "operations-1-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jsonl").symlink_to(outside)
        self.assertEqual([e["message"] for e in read_logs(self.directory)], ["complete"])
        self.assertEqual(outside.read_text(), "not a log")

    def test_configuration_does_not_duplicate_handler(self):
        logger = configure_logging(self.directory)
        try:
            logger = configure_logging(self.directory)
            logger.info("one")
            self.assertEqual(len([h for h in logger.handlers if isinstance(h, JsonLogHandler)]), 1)
            self.assertEqual(len(list(read_logs(self.directory))), 1)
        finally:
            for handler in logger.handlers[:]:
                if isinstance(handler, JsonLogHandler):
                    logger.removeHandler(handler)
                    handler.close()

    def test_invalid_limits_and_naive_filter_time_are_rejected(self):
        for kwargs in ({"max_bytes": 0}, {"file_bytes": 0}, {"max_age_days": 0}):
            with self.subTest(kwargs=kwargs):
                self.assertRaises(ValueError, JsonLogHandler, self.directory, **kwargs)
        self.assertRaises(ValueError, list, read_logs(self.directory, since=datetime(2026, 1, 1)))


if __name__ == "__main__":
    unittest.main()
