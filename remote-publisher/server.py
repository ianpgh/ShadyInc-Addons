"""Single-instance HTTPS publisher behind a managed reverse proxy."""
import base64
from datetime import datetime, timezone
from functools import wraps
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from threading import BoundedSemaphore, Lock
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from publishing import (
    CHANNELS, CHECK_ERRORS, LIMIT, MANAGER, Rejected, apply, check_manifest, checkout, commit_push,
    digest, git, inspect, manager_fields, manifest, preview, publication_state, release_dir, release_reference,
    require, valid_id, verify_public, version,
)

ORIGIN = os.getenv("PUBLIC_ORIGIN", "").rstrip("/")
CLIENT = os.getenv("GITHUB_CLIENT_ID", "")
SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
OWNER = 191883860
SESSION_TTL = 1800
STORE = None
PERSISTENT_TTL = 90 * 86400
WEB = Path(__file__).with_name("web")
ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/style.css": ("style.css", "text/css"), "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
          "/sw.js": ("sw.js", "text/javascript"), "/icon.png": ("icon.png", "image/png"),
          "/icon-512.png": ("icon-512.png", "image/png")}
STATE = {}
SESSIONS = {}
GUARD = Lock()
REPO_OPERATION = BoundedSemaphore(2)
WRITER = Lock()
SHA = re.compile(r"[0-9a-f]{40}")
CHECKSUM = re.compile(r"[0-9a-f]{64}")
VERSION = "1.2.0"
COMMIT_URL = "https://github.com/ianpgh/ShadyInc-Addons/commit/"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def github(url, form=None, token=None):
    headers = {"Accept": "application/json", "User-Agent": "Shady-Remote-Publisher/1.0.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = urlencode(form).encode() if form else None
    with build_opener(NoRedirect).open(Request(url, data, headers), timeout=12) as response:
        return json.loads(response.read(512 * 1024))


def audit(action, status, **values):
    print(json.dumps({"event": action, "status": status, "actor_id": OWNER,
                      "at": datetime.now(timezone.utc).isoformat(), **values}), flush=True)


def complete(state):
    return state["asset"] == "matching" and state["metadata"] and state["catalog"]


def bounded(method):
    @wraps(method)
    def wrapped(self, *args):
        require(REPO_OPERATION.acquire(blocking=False), "Publisher is busy. Retry shortly.")
        try:
            return method(self, *args)
        finally:
            REPO_OPERATION.release()
    return wrapped


class Handler(BaseHTTPRequestHandler):
    server_version = f"ShadyRemotePublisher/{VERSION}"
    timeout = 20

    def log_message(self, format, *args):
        # Request targets can contain authorization codes.
        return

    def send(self, status, body=b"", mime="text/plain", **headers):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "img-src 'self'; connect-src 'self'; worker-src 'self'; manifest-src 'self'; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        if getattr(self, "renew_cookie", False) and "Set_Cookie" not in headers:
            self.send_header("Set-Cookie", f"__Host-session={self.cookie('__Host-session')}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age={PERSISTENT_TTL}")
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def json(self, status, obj):
        self.send(status, json.dumps(obj).encode(), "application/json")

    def cookie(self, name):
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        return cookies[name].value if name in cookies else ""

    def redirect(self, target, name, value, ttl):
        self.send(303, Location=target,
                  Set_Cookie=f"__Host-{name}={value}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age={ttl}")

    def prune(self):
        now = time.monotonic()
        for table in (STATE, SESSIONS):
            for key, value in list(table.items()):
                if value["expires"] <= now:
                    table.pop(key)

    def require_active(self, session):
        active = self.session()
        require(active and hmac.compare_digest(active["csrf"], session["csrf"]) if STORE is not None
                else active is session, "Session expired. Sign in again before publishing.")
        if STORE is not None:
            session["token"] = active["token"]

    def session(self):
        sid = self.cookie("__Host-session")
        if STORE is not None:
            active, renewed = self.store_call(STORE.load, sid, self.refresh_credentials)
            if renewed:
                self.renew_cookie = True
            if not active:
                return None
            with GUARD:
                self.prune()
                # Validation tickets deliberately remain process-local and require revalidation after restart.
                cached = SESSIONS.setdefault(sid, {"tickets": {}, "expires": time.monotonic() + SESSION_TTL})
                cached["expires"] = time.monotonic() + SESSION_TTL
                active["tickets"] = cached["tickets"]
            return active
        with GUARD:
            self.prune()
            return SESSIONS.get(sid)

    def login(self):
        require(CLIENT and SECRET and ORIGIN, "GitHub App authentication is not configured.")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        with GUARD:
            self.prune()
            require(len(STATE) < 128, "Sign-in capacity reached. Retry shortly.")
            STATE[state] = {"verifier": verifier, "expires": time.monotonic() + 300}
        query = urlencode({"client_id": CLIENT, "redirect_uri": ORIGIN + "/oauth/callback",
                           "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
                           "scope": "offline_access"})
        self.redirect("https://github.com/login/oauth/authorize?" + query, "oauth", state, 300)

    def callback(self, query):
        code = query.get("code", [""])[0]
        state = query.get("state", [""])[0]
        require(code and state and hmac.compare_digest(state, self.cookie("__Host-oauth")), "Invalid sign-in state.")
        with GUARD:
            self.prune()
            pending = STATE.pop(state, None)
        require(pending, "Sign-in expired.")
        auth = github("https://github.com/login/oauth/access_token",
                      {"client_id": CLIENT, "client_secret": SECRET, "code": code,
                       "redirect_uri": ORIGIN + "/oauth/callback", "code_verifier": pending["verifier"]})
        token = auth.get("access_token")
        require(token and 0 < auth.get("expires_in", 0) <= 28800, "The GitHub App must issue expiring tokens.")
        self.check_owner(token)
        if STORE is not None:
            sid = secrets.token_urlsafe(32)
            self.store_call(STORE.create, sid, self.cookie("__Host-session"), auth, secrets.token_urlsafe(32))
            self.redirect("/", "session", sid, PERSISTENT_TTL)
            return
        ttl = min(SESSION_TTL, auth["expires_in"])
        sid = secrets.token_urlsafe(32)
        with GUARD:
            SESSIONS.pop(self.cookie("__Host-session"), None)
            require(len(SESSIONS) < 16, "Sign-in capacity reached.")
            SESSIONS[sid] = {"expires": time.monotonic() + ttl, "token": token,
                             "csrf": secrets.token_urlsafe(32), "tickets": {}}
        self.redirect("/", "session", sid, ttl)

    @staticmethod
    def check_owner(token):
        user = github("https://api.github.com/user", token=token)
        require(user.get("id") == OWNER, "This GitHub account is not authorized.")
        repos = github("https://api.github.com/user/installations", token=token)
        require(len(repos.get("installations", [])) == 1, "Install this private GitHub App on the addon repository.")
        installation = repos["installations"][0]["id"]
        allowed = github(f"https://api.github.com/user/installations/{installation}/repositories", token=token)
        require(allowed.get("total_count") == 1 and allowed["repositories"][0]["id"] == 1470523949,
                "The GitHub App installation must select only ShadyInc-Addons.")

    def refresh_credentials(self, refresh):
        auth = github("https://github.com/login/oauth/access_token",
                      {"client_id": CLIENT, "client_secret": SECRET,
                       "grant_type": "refresh_token", "refresh_token": refresh})
        if auth.get("error") in {"bad_refresh_token", "invalid_grant", "expired_token"}:
            return None
        require(auth.get("access_token"), "GitHub credential renewal unavailable. Retry shortly.")
        return auth

    @staticmethod
    def store_call(method, *args):
        try:
            return method(*args)
        except Rejected:
            raise
        except Exception as exc:
            # Never return or log connection strings or credentials.
            audit("session_store", "unavailable", error=type(exc).__name__)
            raise OSError("Session storage temporarily unavailable.") from None

    def read_body(self, limit, mime):
        require(not self.headers.get("Transfer-Encoding") and len(self.headers.get_all("Content-Length", [])) == 1,
                "Invalid request framing.")
        require(self.headers.get("Content-Type") == mime, "Invalid request media type.")
        size = int(self.headers["Content-Length"])
        require(0 <= size <= limit, "Request exceeds the upload size limit.")
        body = self.rfile.read(size)
        require(len(body) == size, "Upload interrupted.")
        return body

    @bounded
    def history(self):
        with checkout(full=True) as work:
            head = git(work, "rev-parse", "HEAD").decode().strip()
            logs = git(work, "log", "-100", "--format=%H%x09%cI%x09%an%x09%s", "--", "releases").decode()
        rows = []
        for line in logs.splitlines():
            sha, date, source, title = line.split("\t", 3)
            match = re.fullmatch(r"(Publish|Rollback) ([A-Za-z][A-Za-z0-9_-]{0,63}) (.+)", title)
            if not match:
                continue
            action, addon, release = match.groups()
            try:
                channel, _ = version(release)
            except Rejected:
                continue
            rows.append({"sha": sha, "date": date, "source": "Remote Publisher" if source == "Shady Remote Publisher"
                         else "Git commit · " + source, "action": action, "addon": addon,
                         "version": release, "channel": channel, "link": COMMIT_URL + sha})
        return {"head": head, "rows": rows}

    @bounded
    def validate(self, session):
        selected = self.headers.get("X-Release-Channel", "auto")
        data = self.read_body(LIMIT, "application/zip")
        package = inspect(data, selected)
        with checkout() as work:
            state = publication_state(work, release_reference(package), CHANNELS[package["channel"]])
            if complete(state):
                return {**package, "alreadyPublished": True}
            result = preview(work, package, resume=True)
        ticket = secrets.token_urlsafe(24)
        with GUARD:
            if len(session["tickets"]) >= 50:
                session["tickets"].pop(next(iter(session["tickets"])))
            session["tickets"][ticket] = (package["sha256"], selected, result["current"], result["publicStable"])
        return {**result, "ticket": ticket}

    def receipt(self, fields, filename, sha, pushed):
        response = {"addon": fields.get("addon", fields.get("manager")), "version": fields["version"],
                    "channel": version(fields["version"])[0], "manifest": filename, "sha": sha,
                    "link": COMMIT_URL + sha}
        audit("mutation", "accepted" if pushed else "unconfirmed", **response)
        return {**response, "status": "verification_pending" if pushed else "push_unconfirmed"}

    @bounded
    def publish(self, session):
        require(self.headers.get("X-Release-Confirmation") == "publish", "Explicit publish confirmation required.")
        require(WRITER.acquire(blocking=False), "Another repository write is in progress. Verify Publication before retrying.")
        try:
            return self.publish_locked(session)
        finally:
            WRITER.release()

    def publish_locked(self, session):
        data = self.read_body(LIMIT, "application/zip")
        ticket = self.headers.get("X-Validation-Ticket", "")
        with GUARD:
            binding = session["tickets"].get(ticket)
            require(binding and hmac.compare_digest(binding[0], digest(data)),
                    "Validate these exact ZIP bytes before publishing.")
            session["tickets"].pop(ticket)
        package = inspect(data, binding[1])
        fields = release_reference(package)
        filename = CHANNELS[package["channel"]]
        context = {"addon": package["addon"], "version": package["version"], "channel": package["channel"]}
        sha = None
        for attempt in (1, 2, 3):
            with checkout() as work:
                state = publication_state(work, fields, filename)
                if complete(state):
                    sha = git(work, "rev-parse", "HEAD").decode().strip()
                    audit("publish", "already_on_github", sha=sha, attempt=attempt, **context)
                    return self.receipt(fields, filename, sha, True)
                if attempt == 3:
                    break
                review = preview(work, package, resume=True)
                require((review["current"], review["publicStable"]) == binding[2:] or state["metadata"],
                        "The reviewed channel changed. Validate the package again.")
                paths = apply(work, package, data, resume=True)[1]
                self.require_active(session)
                sha, pushed = commit_push(work, paths, f"Publish {package['addon']} {package['version']}", session["token"])
                if pushed:
                    return self.receipt(fields, filename, sha, True)
            audit("publish", "push_response_unconfirmed", sha=sha, attempt=attempt, **context)
            time.sleep(1)
        return self.receipt(fields, filename, sha, False)

    def verification_reference(self):
        data = json.loads(self.read_body(4096, "application/json"))
        addon, release, channel, checksum = (data[key] for key in ("addon", "version", "channel", "sha256"))
        require(all(isinstance(value, str) for value in (addon, release, channel, checksum)) and
                channel in CHANNELS and version(release)[0] == channel and CHECKSUM.fullmatch(checksum),
                "Invalid publication reference.")
        if addon == MANAGER:
            fields = manager_fields({"manager": addon, "version": release, "sha256": checksum,
                                     "url": f"ShadyAddonManager_{release}_windows_amd64.exe"})
        else:
            require(valid_id(addon), "Invalid publication reference.")
            fields = {"addon": addon, "version": release, "sha256": checksum,
                      "url": f"{addon}_v{release.replace(' ', '_')}.zip"}
        return addon, channel, fields

    @bounded
    def verify_publication(self):
        addon, channel, fields = self.verification_reference()
        filename = CHANNELS[channel]
        context = {"addon": addon, "version": fields["version"], "channel": channel}

        def result(status, detail, **values):
            audit("verify_publication", status, **context, detail=detail, **values)
            return {"status": status, "detail": detail, **values}

        if WRITER.locked():
            return result("publishing", "A repository write is in progress. Use Verify Publication to check again.")
        try:
            with checkout() as work:
                sha = git(work, "rev-parse", "HEAD").decode().strip()
                state = publication_state(work, fields, filename)
        except CHECK_ERRORS as exc:
            return result("verifying", f"GitHub could not be checked ({type(exc).__name__}). Use Verify Publication to check again.")

        if not complete(state):
            if WRITER.locked():
                return result("publishing", "A repository write is in progress. Use Verify Publication to check again.")
            asset, metadata = state["asset"], state["metadata"]
            live = state["live"]
            if asset == "conflict" or (live and not metadata and live.get("version") == fields["version"]):
                detail = "GitHub already has conflicting data for this version. Check the filename and checksum or use a new version."
                return result("partial" if metadata or asset == "matching" else "failed", detail, retryable=False)
            if asset == "matching" and not metadata and live:
                live_channel, live_version = version(live.get("version", ""))
                if live_channel == channel and live_version >= version(fields["version"])[1]:
                    return result("partial", f"The ZIP is on GitHub. The channel currently points to {live['version']}. Review release history.",
                                  retryable=False)
            if asset == "matching" or metadata:
                missing = []
                if asset == "missing":
                    missing.append("the download file")
                if not metadata:
                    missing.append("matching channel metadata")
                if not state["catalog"]:
                    missing.append("the catalog entry")
                return result("partial", "GitHub is missing " + ", ".join(missing) +
                              ". Validate the same ZIP to repair an incomplete release. Use History for earlier releases.",
                              retryable=True)
            return result("absent", "GitHub has no matching file or channel metadata. Validate the same ZIP before retrying.",
                          retryable=True)

        try:
            download = verify_public(addon, filename, state["live"], sha, catalog_required=True)
        except Rejected as exc:
            return result("partial", f"GitHub has the file and metadata. {exc} Use Verify Publication to recheck the download.",
                          sha=sha, link=COMMIT_URL + sha, retryable=False)
        return result("published", "GitHub metadata and the public download checksum match.",
                      sha=sha, link=COMMIT_URL + sha, download=download)

    def read_operation(self):
        data = json.loads(self.read_body(4096, "application/json"))
        addon = data["addon"]
        channel = data["channel"]
        sha = data["sha"]
        require((valid_id(addon) or (addon == MANAGER and channel == "stable")) and channel in CHANNELS and SHA.fullmatch(sha), "Invalid release reference.")
        return addon, channel, sha

    def historical(self, work, addon, filename, sha):
        git(work, "merge-base", "--is-ancestor", sha, "HEAD")
        return json.loads(git(work, "show", f"{sha}:releases/{addon}/{filename}"))

    @bounded
    def check(self):
        addon, channel, sha = self.read_operation()
        filename = CHANNELS[channel]
        with checkout(full=True) as work:
            folder = release_dir(work, addon)
            fields = check_manifest(folder, self.historical(work, addon, filename, sha))
            require(version(fields["version"])[0] == channel, "The commit has a different release channel.")
            require(manifest(folder, filename) == fields, "The release has been superseded.")
        download = verify_public(addon, filename, fields, sha)
        audit("verify", "verified", addon=addon, version=fields["version"], channel=channel, sha=sha)
        return {"status": "verified", "download": download, "link": COMMIT_URL + sha}

    @bounded
    def rollback(self, session):
        addon, channel, prior_sha = self.read_operation()
        require(addon != MANAGER, "Manager rollback is not supported.")
        require(self.headers.get("X-Release-Confirmation") == "rollback", "Explicit rollback confirmation required.")
        filename = CHANNELS[channel]
        require(WRITER.acquire(blocking=False), "Another publish is in progress.")
        try:
            with checkout(full=True) as work:
                require(git(work, "rev-parse", "HEAD").decode().strip() == self.headers.get("X-Reviewed-Head"),
                        "Repository changed since history was reviewed. Refresh before rollback.")
                folder = release_dir(work, addon)
                current = manifest(folder, filename, optional=False)
                fields = check_manifest(folder, self.historical(work, addon, filename, prior_sha))
                old_channel, old_version = version(fields["version"])
                live_channel, live_version = version(current["version"])
                require(old_channel == live_channel == channel and old_version < live_version,
                        "Rollback requires an earlier release in the same channel.")
                path = folder / filename
                path.write_bytes(git(work, "show", f"{prior_sha}:releases/{addon}/{filename}"))
                self.require_active(session)
                sha, pushed = commit_push(work, [path], f"Rollback {addon} {fields['version']}", session["token"])
        finally:
            WRITER.release()
        return self.receipt(fields, filename, sha, pushed)

    def do_GET(self):
        try:
            url = urlsplit(self.path)
            if url.path in ASSETS:
                name, mime = ASSETS[url.path]
                self.send(200, (WEB / name).read_bytes(), mime)
            elif url.path == "/login":
                self.login()
            elif url.path == "/oauth/callback":
                self.callback(parse_qs(url.query))
            elif url.path == "/api/session":
                session = self.session()
                self.json(200, {"authenticated": bool(session), "csrf": session["csrf"] if session else None,
                                "configured": bool(ORIGIN and CLIENT and SECRET)})
            elif url.path == "/api/history" and self.session():
                self.json(200, self.history())
            else:
                self.json(404, {"error": "Page unavailable."})
        except Rejected as exc:
            self.json(400, {"error": str(exc)})
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            self.json(502, {"error": "GitHub or the repository could not be reached."})

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            session = self.session()
            if not session:
                return self.json(401, {"error": "Sign in again to continue."})
            if (self.headers.get("Origin") != ORIGIN or
                    not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"])):
                return self.json(403, {"error": "Invalid request origin or session proof."})
            if path == "/api/logout":
                if STORE is not None:
                    self.store_call(STORE.delete, self.cookie("__Host-session"))
                with GUARD:
                    SESSIONS.pop(self.cookie("__Host-session"), None)
                return self.send(204, Set_Cookie="__Host-session=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0")
            routes = {"/api/validate": lambda: self.validate(session), "/api/publish": lambda: self.publish(session),
                      "/api/check": self.check, "/api/verify-publication": self.verify_publication,
                      "/api/rollback": lambda: self.rollback(session)}
            if path not in routes:
                return self.json(404, {"error": "Unknown endpoint."})
            result = routes[path]()
        except Rejected as exc:
            if path in {"/api/publish", "/api/rollback", "/api/verify-publication"}:
                audit(path, "rejected", detail=str(exc))
            return self.json(409, {"error": str(exc)})
        except CHECK_ERRORS as exc:
            audit(path, "unconfirmed", error=type(exc).__name__)
            return self.json(502, {"error": "GitHub or the request could not be checked. Use Verify Publication before retrying."})
        try:
            self.json(200, result)
        except (BrokenPipeError, ConnectionResetError, TimeoutError) as exc:
            audit(path, "response_disconnected", error=type(exc).__name__)


class HTTPServer(ThreadingHTTPServer):
    capacity = BoundedSemaphore(16)

    def process_request(self, request, address):
        if not self.capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.capacity.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.capacity.release()


if __name__ == "__main__":
    if ORIGIN and not re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]+)?", ORIGIN):
        raise SystemExit("PUBLIC_ORIGIN must be a bare HTTPS origin.")
    if ORIGIN and CLIENT and SECRET:
        from session_store import SessionStore
        try:
            STORE = SessionStore(os.environ["DATABASE_URL"], os.environ["SESSION_ENCRYPTION_KEY"])
            STORE.initialize()
        except Exception as exc:
            audit("session_store", "startup_failed", error=type(exc).__name__)
            raise SystemExit("Persistent session storage configuration or connection failed.") from None
        audit("session_store", "ready")
    HTTPServer(("0.0.0.0", int(os.getenv("PORT", 8080))), Handler).serve_forever()
