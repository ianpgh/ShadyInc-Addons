"""Isolated tests. No test writes to GitHub or contacts production."""
import io
from email.message import Message
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from threading import Thread
import zipfile

import publishing as pub
import server

COMPILER = os.getenv("TEST_LUAC", "/usr/bin/luac5.1")


def package(release="1.0.0", extra=(), toc="Addon.lua", lua="return true", addon="Fixture"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{addon}/{addon}.toc", f"## Interface: 11200\n## Version: {release}\n"
                         f"## X-Shady-Changelog: First; Second\n{toc}\n")
        archive.writestr(f"{addon}/Addon.lua", lua)
        for path, data in extra:
            archive.writestr(path, data)
    return buffer.getvalue()


def inspect(data, channel="auto"):
    return pub.inspect(data, channel, compiler=COMPILER)


class ValidatorTests(unittest.TestCase):
    def test_valid_exact_bytes_and_metadata(self):
        data = package("2.0 Alpha 3")
        result = inspect(data, "alpha")
        self.assertEqual(result["sha256"], pub.digest(data))
        self.assertEqual(result["url"], "Fixture_v2.0_Alpha_3.zip")
        self.assertEqual(result["changelog"], ["First", "Second"])

    def test_invalid_archives(self):
        symlink = zipfile.ZipInfo("Fixture/link.lua")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = [
            b"invalid",
            package(extra=[("Fixture/../Other.lua", "x")]),
            package(extra=[("Fixture/addon.lua", "x")]),
            package(extra=[("Fixture/CON.txt", "x")]),
            package(extra=[(symlink, "Addon.lua")]),
            package(extra=[("Fixture/extra.zip", "x")]),
            package(extra=[("Fixture/readme.txt", b"MZ\x00")]),
            package(extra=[("Fixture/run.exe", "x")]),
            package(extra=[("Fixture/packed.txt", "x" * 2_000_000)]),
            package(extra=[("Fixture/Addon.lua/child.txt", "x")]),
            package(extra=[("Fixture/Beta.toc", "## Version: 2.0.0")]),
            package(toc="Missing.lua"),
            package(lua="local ="),
            package(addon="ShadyDesktopManager"),
        ]
        for i, data in enumerate(cases):
            with self.subTest(case=i), self.assertRaises(pub.Rejected):
                inspect(data)

    def test_channel_mismatch(self):
        with self.assertRaisesRegex(pub.Rejected, "conflicts"):
            inspect(package("2.0.0-beta.1"), "stable")

    def test_lua_51_upvalue_limit(self):
        names = [f"v{i}" for i in range(61)]
        lua = "local " + ",".join(names) + "\nreturn function() return " + ",".join(names) + " end"
        with self.assertRaisesRegex(pub.Rejected, "more than 60 upvalues"):
            inspect(package(lua=lua))


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = str(self.root / "remote.git")
        pub.git(self.root, "init", "--bare", "--initial-branch=main", self.remote)
        seed = self.root / "seed"
        seed.mkdir()
        pub.git(seed, "init", "--initial-branch=main")
        (seed / "releases").mkdir()
        (seed / "releases" / "README.md").write_text("seed")
        (seed / "catalog.json").write_text('[{"id":"Fixture","name":"Fixture","custom":true}]\n')
        pub.git(seed, "add", ".")
        pub.git(seed, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "Seed")
        pub.git(seed, "push", self.remote, "main")

    def publish_local(self, release, addon="Fixture"):
        data = package(release, addon=addon)
        with pub.checkout(self.remote) as work:
            parsed = inspect(data)
            _, paths = pub.apply(work, parsed, data)
            head, pushed = pub.commit_push(work, paths, f"Publish {addon} {release}")
            self.assertTrue(pushed)
            return head

    def test_independent_channels_and_catalog(self):
        self.publish_local("1.0.0")
        with pub.checkout(self.remote) as work:
            stable = (work / "releases/Fixture/latest.json").read_bytes()
            catalog = (work / "catalog.json").read_bytes()
        self.publish_local("2.0 Alpha 3")
        self.publish_local("2.0.0-beta.1")
        with pub.checkout(self.remote) as work:
            folder = work / "releases/Fixture"
            self.assertEqual(stable, (folder / "latest.json").read_bytes())
            self.assertEqual(catalog, (work / "catalog.json").read_bytes())
            self.assertEqual(pub.manifest(folder, "alpha.json")["version"], "2.0 Alpha 3")
            self.assertEqual(pub.manifest(folder, "beta.json")["version"], "2.0.0-beta.1")
        self.publish_local("2.0.0")
        with pub.checkout(self.remote) as work:
            folder = work / "releases/Fixture"
            self.assertEqual(pub.manifest(folder, "latest.json")["version"], "2.0.0")
            self.assertTrue((folder / "Fixture_v1.0.0.zip").is_file())

    def test_stale_checkout_and_duplicate(self):
        with pub.checkout(self.remote) as stale:
            data = package("2.0.0")
            _, paths = pub.apply(stale, inspect(data), data)
            self.publish_local("3.0.0")
            with self.assertRaisesRegex(pub.Rejected, "Repository changed"):
                pub.commit_push(stale, paths, "Publish Fixture 2.0.0")
        with pub.checkout(self.remote) as work:
            with self.assertRaisesRegex(pub.Rejected, "downgrade"):
                pub.preview(work, inspect(data))
            with self.assertRaisesRegex(pub.Rejected, "already exists"):
                pub.preview(work, inspect(package("3.0.0")))

    def test_competing_push_between_head_check_and_push(self):
        data = package("2.0.0")
        with pub.checkout(self.remote) as losing:
            _, paths = pub.apply(losing, inspect(data), data)
            original_git = pub.git

            def race(work, *args, **kwargs):
                if work == losing and args[0] == "push":
                    self.publish_local("3.0.0")
                return original_git(work, *args, **kwargs)

            with patch.object(pub, "git", side_effect=race):
                _, pushed = pub.commit_push(losing, paths, "Publish Fixture 2.0.0")
            self.assertFalse(pushed)
        with pub.checkout(self.remote) as work:
            self.assertEqual(pub.manifest(work / "releases/Fixture", "latest.json")["version"], "3.0.0")

    def test_push_interrupted_before_commit_is_visible(self):
        data = package("2.0.0")
        with pub.checkout(self.remote) as work:
            original = pub.git(work, "rev-parse", "HEAD")
            _, paths = pub.apply(work, inspect(data), data)
            call = pub.git

            def unavailable(work, *args, **kwargs):
                if args[0] == "push":
                    raise pub.Rejected("Simulated connection loss.")
                return call(work, *args, **kwargs)

            with patch.object(pub, "git", side_effect=unavailable):
                sha, pushed = pub.commit_push(work, paths, "Publish Fixture 2.0.0")
            self.assertFalse(pushed)
            self.assertTrue(sha)
        with pub.checkout(self.remote) as work:
            self.assertEqual(pub.git(work, "rev-parse", "HEAD"), original)
            self.assertFalse((work / "releases/Fixture/latest.json").exists())

    def test_atomic_rollback_preserves_assets(self):
        old = self.publish_local("1.0.0")
        self.publish_local("2.0.0")
        with pub.checkout(self.remote, full=True) as work:
            folder = work / "releases/Fixture"
            asset = folder / "Fixture_v2.0.0.zip"
            before = asset.read_bytes()
            path = folder / "latest.json"
            path.write_bytes(pub.git(work, "show", f"{old}:releases/Fixture/latest.json"))
            _, pushed = pub.commit_push(work, [path], "Rollback Fixture 1.0.0")
            self.assertTrue(pushed)
        with pub.checkout(self.remote) as work:
            self.assertEqual(pub.manifest(work / "releases/Fixture", "latest.json")["version"], "1.0.0")
            self.assertEqual((work / "releases/Fixture/Fixture_v2.0.0.zip").read_bytes(), before)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = server.HTTPServer(("127.0.0.1", 0), server.Handler)
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def test_unauthorized_publish_and_security_headers(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.origin + "/api/publish", b"zip", method="POST"))
        self.assertEqual(error.exception.code, 401)
        self.assertIn("frame-ancestors 'none'", error.exception.headers["Content-Security-Policy"])
        self.assertEqual(error.exception.headers["Cache-Control"], "no-store")

    def test_interrupted_upload_rejected_before_validation(self):
        handler = server.Handler.__new__(server.Handler)
        handler.rfile = io.BytesIO(b"abc")
        handler.headers = Message()
        handler.headers["Content-Type"] = "application/zip"
        handler.headers["Content-Length"] = "20"
        with self.assertRaisesRegex(pub.Rejected, "Upload interrupted"):
            handler.read_body(100, "application/zip")

    def test_oauth_rejects_wrong_owner_and_consumes_state(self):
        import time
        state = "test-oauth-state"
        server.STATE[state] = {"verifier": "test", "expires": time.monotonic() + 20}
        handler = server.Handler.__new__(server.Handler)
        handler.headers = Message()
        handler.headers["Cookie"] = f"__Host-oauth={state}"
        query = {"code": ["test"], "state": [state]}
        with patch.object(server, "github", side_effect=[
                {"access_token": "mock", "expires_in": 100}, {"id": -1}]):
            with self.assertRaisesRegex(pub.Rejected, "not authorized"):
                handler.callback(query)
        self.assertNotIn(state, server.STATE)
        with self.assertRaisesRegex(pub.Rejected, "expired"):
            handler.callback(query)

    def test_authenticated_cross_origin_rejected(self):
        import time
        sid = "test-cross-origin"
        server.SESSIONS[sid] = {"csrf": "correct", "expires": time.monotonic() + 20}
        self.addCleanup(server.SESSIONS.pop, sid)
        headers = {"Cookie": f"__Host-session={sid}", "X-CSRF-Token": "correct", "Origin": "https://other.example"}
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.origin + "/api/logout", b"{}", headers))
        self.assertEqual(error.exception.code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
