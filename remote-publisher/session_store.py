"""Durable owner-only sessions; database contains no plaintext credentials."""
import base64
import hashlib
import json
import os
import time

import psycopg
from cryptography.fernet import Fernet
from psycopg.rows import dict_row

TTL = 90 * 86400
RENEW_INTERVAL = 86400


class SessionUnavailable(OSError):
    """A transient storage or refresh failure is not an expired login."""


class SessionStore:
    def __init__(self, url, key):
        self.url = url
        raw = base64.b64decode(key, validate=True)
        if len(raw) != 32:
            raise ValueError("SESSION_ENCRYPTION_KEY must encode 32 bytes")
        self.cipher = Fernet(base64.urlsafe_b64encode(raw))

    def connect(self):
        return psycopg.connect(self.url, connect_timeout=10, row_factory=dict_row,
                               prepare_threshold=None)

    @staticmethod
    def sid_hash(sid):
        return hashlib.sha256(sid.encode()).hexdigest()

    def initialize(self):
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS shady_sessions (
                sid_hash text PRIMARY KEY, expires bigint NOT NULL,
                payload bytea NOT NULL)""")

    def seal(self, payload):
        return self.cipher.encrypt(json.dumps(payload).encode())

    def open(self, payload):
        return json.loads(self.cipher.decrypt(bytes(payload)))

    def create(self, sid, old_sid, auth, csrf):
        now = int(time.time())
        payload = self.credentials(auth, now)
        payload["csrf"] = csrf
        with self.connect() as conn:
            conn.execute("DELETE FROM shady_sessions WHERE expires <= %s OR sid_hash = %s",
                         (now, self.sid_hash(old_sid)))
            conn.execute("SELECT pg_advisory_xact_lock(191883860)")
            count = conn.execute("SELECT count(*) AS n FROM shady_sessions").fetchone()["n"]
            if count >= 16:
                raise SessionUnavailable("Session capacity reached; sign out on another device.")
            conn.execute("INSERT INTO shady_sessions VALUES (%s,%s,%s)",
                         (self.sid_hash(sid), now + TTL, self.seal(payload)))

    @staticmethod
    def credentials(auth, now):
        access, refresh = auth.get("access_token"), auth.get("refresh_token")
        lifetime, refresh_lifetime = auth.get("expires_in"), auth.get("refresh_token_expires_in")
        if (not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh
                or not isinstance(lifetime, int) or not 0 < lifetime <= 28800
                or not isinstance(refresh_lifetime, int) or refresh_lifetime <= 0):
            raise SessionUnavailable("GitHub did not provide renewable credentials.")
        return {"token": access, "refresh": refresh, "token_expires": now + lifetime,
                "refresh_expires": now + refresh_lifetime}

    def load(self, sid, refresh_callback, renew=True):
        if not sid or len(sid) > 128:
            return None, False
        now = int(time.time())
        key = self.sid_hash(sid)
        with self.connect() as conn:
            row = conn.execute("SELECT expires,payload FROM shady_sessions WHERE sid_hash=%s FOR UPDATE",
                               (key,)).fetchone()
            if not row:
                return None, False
            if row["expires"] <= now:
                conn.execute("DELETE FROM shady_sessions WHERE sid_hash=%s", (key,))
                return None, False
            payload = self.open(row["payload"])
            changed = False
            if payload["token_expires"] <= now + 120:
                if payload["refresh_expires"] <= now:
                    conn.execute("DELETE FROM shady_sessions WHERE sid_hash=%s", (key,))
                    return None, False
                auth = refresh_callback(payload["refresh"])
                if auth is None:
                    conn.execute("DELETE FROM shady_sessions WHERE sid_hash=%s", (key,))
                    return None, False
                payload.update(self.credentials(auth, int(time.time())))
                changed = True
            renewed = renew and row["expires"] < now + TTL - RENEW_INTERVAL
            if changed or renewed:
                conn.execute("UPDATE shady_sessions SET expires=%s,payload=%s WHERE sid_hash=%s",
                             (now + TTL if renewed else row["expires"], self.seal(payload), key))
            return {"token": payload["token"], "csrf": payload["csrf"]}, renewed

    def delete(self, sid):
        with self.connect() as conn:
            conn.execute("DELETE FROM shady_sessions WHERE sid_hash=%s", (self.sid_hash(sid),))
