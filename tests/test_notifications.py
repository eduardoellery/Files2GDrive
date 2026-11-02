import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from files2gdrive.notifications import NotificationPolicy, desktop_status
from files2gdrive.drive import _safe_error


class NotificationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.state = Path(self.temp.name)
        self.sender = Mock(return_value=True); self.now = 100
        self.policy = NotificationPolicy(self.state, self.sender, clock=lambda: self.now)
    def tearDown(self): self.temp.cleanup()
    def status(self, **issues):
        return dict(running=True, auth_state='required' if issues.get('auth') else 'connected', issues=issues)
    def test_notify_once_until_problem_resolves(self):
        status = self.status(auth=True)
        self.policy(status); self.policy(status)
        self.assertEqual(self.sender.call_count, 1)
        self.policy(self.status()); self.policy(status)
        self.assertEqual(self.sender.call_count, 2)
    def test_restart_does_not_repeat_unresolved_notification(self):
        self.policy(self.status(quota=True))
        restarted = NotificationPolicy(self.state, self.sender, clock=lambda: self.now)
        restarted(self.status(quota=True))
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual((self.state / 'desktop-notifications.json').stat().st_mode & 0o777, 0o600)
    def test_failed_delivery_backoff_and_unknown_connection(self):
        self.sender.return_value = False
        self.policy(self.status(auth=True)); self.policy(self.status(auth=True))
        self.assertEqual(self.sender.call_count, 1)
        self.now += 61; self.sender.return_value = True
        self.policy(self.status(auth=True))
        self.policy(dict(running=True, auth_state='unknown', issues={}))
        self.policy(self.status(auth=True))
        self.assertEqual(self.sender.call_count, 2)
    def test_desktop_snapshot_does_not_include_paths_or_task_data(self):
        result = desktop_status(dict(running=True, auth_state='connected',
            error_counts={'quota': 1}, roots=[dict(enabled=1, status='unavailable', path='/private')],
            task={'result':'secret'}, queue=[]))
        self.assertTrue(result['issues']['quota']); self.assertEqual(result['issues']['folders'],1)
        self.assertNotIn('private',json.dumps(result)); self.assertNotIn('secret',json.dumps(result))
    def test_only_storage_quota_error_not_rate_limit_is_space_alert(self):
        class Failure(Exception):
            resp=type('Response',(),{'status':403})()
        for reason, expected in [('storageQuotaExceeded','quota'),('userRateLimitExceeded','unknown'),('domainPolicy','access')]:
            error=Failure('SECRET'); error.content=json.dumps({'error':{'errors':[{'reason':reason}]}}).encode()
            safe=_safe_error(error)
            self.assertEqual(safe.category,expected); self.assertNotIn('SECRET',str(safe))

    def test_corrupted_nonobject_notification_state_is_ignored(self):
        (self.state / 'desktop-notifications.json').write_text('[]')
        policy=NotificationPolicy(self.state,self.sender,clock=lambda:self.now)
        policy(self.status(auth=True))
        self.sender.assert_called_once()
