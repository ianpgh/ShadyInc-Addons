import base64
import os
import secrets
import time
import unittest
from session_store import SessionStore

@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "disposable PostgreSQL required")
class DurableSessionTests(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        self.key = base64.b64encode(secrets.token_bytes(32)).decode()
        self.store = SessionStore(self.url, self.key)
        self.store.initialize()
        self.sid = secrets.token_urlsafe(32)
        self.addCleanup(self.store.delete, self.sid)

    def auth(self, access="access", refresh="refresh", lifetime=3600):
        return dict(access_token=access, refresh_token=refresh, expires_in=lifetime,
                    refresh_token_expires_in=86400)

    def test_restart_refresh_and_logout(self):
        self.store.create(self.sid, "", self.auth(lifetime=1), "csrf")
        restarted = SessionStore(self.url, self.key)
        calls = []
        def refresh(old):
            calls.append(old)
            return self.auth("new-access", "new-refresh")
        active, _ = restarted.load(self.sid, refresh)
        self.assertEqual(active, {"token": "new-access", "csrf": "csrf"})
        self.assertEqual(calls, ["refresh"])
        self.assertEqual(restarted.load(self.sid, refresh)[0], active)
        self.assertEqual(len(calls), 1)
        with restarted.connect() as conn:
            row = conn.execute("SELECT payload FROM shady_sessions WHERE sid_hash=%s",
                               (restarted.sid_hash(self.sid),)).fetchone()
            self.assertNotIn(b"new-access", bytes(row["payload"]))
        restarted.delete(self.sid)
        self.assertIsNone(self.store.load(self.sid, refresh)[0])

    def test_transient_failure_preserves_session(self):
        self.store.create(self.sid, "", self.auth(lifetime=1), "csrf")
        def unavailable(old):
            raise OSError("offline")
        with self.assertRaises(OSError):
            self.store.load(self.sid, unavailable)
        self.assertIsNotNone(self.store.load(self.sid, lambda old: self.auth())[0])

    def test_rolling_expiry(self):
        self.store.create(self.sid, "", self.auth(), "csrf")
        with self.store.connect() as conn:
            conn.execute("UPDATE shady_sessions SET expires=%s WHERE sid_hash=%s",
                         (int(time.time()) + 3600, self.store.sid_hash(self.sid)))
        self.assertTrue(self.store.load(self.sid, lambda old: self.auth())[1])

if __name__ == "__main__":
    unittest.main()
