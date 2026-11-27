"""WoW addon archive validation and Git-backed publication."""
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import unicodedata
from urllib.parse import quote
from urllib.request import urlopen
import zipfile

REPO = "https://github.com/ianpgh/ShadyInc-Addons.git"
RAW = "https://raw.githubusercontent.com/ianpgh/ShadyInc-Addons"
PAGES = "https://ianpgh.github.io/ShadyInc-Addons"
LOGGER = logging.getLogger(__name__)
LIMIT = 32 * 1024 * 1024
ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
NUMBER = r"(0|[1-9][0-9]{0,5})"
VERSION = re.compile(rf"{NUMBER}\.{NUMBER}(?:\.{NUMBER})?(?:[ -](alpha|beta)(?:[ .-]([0-9]{{1,6}}))?)?", re.I)
CHANNELS = {"stable": "latest.json", "alpha": "alpha.json", "beta": "beta.json"}
CHECK_ERRORS = (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError)
SAFE_ASSETS = {".toc", ".lua", ".xml", ".txt", ".md", ".blp", ".tga", ".dds",
               ".png", ".jpg", ".jpeg", ".ogg", ".mp3", ".wav", ".ttf", ".otf", ".csv", ".json"}


class Rejected(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Rejected(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def valid_id(value):
    return bool(ID.fullmatch(value)) and value.lower() != "shadydesktopmanager"


def version(value):
    match = VERSION.fullmatch(value)
    require(match and (match[3] is not None or match[4]), "Unsupported TOC version.")
    channel = (match[4] or "stable").lower()
    return channel, tuple(int(match[i] or 0) for i in (1, 2, 3, 5))


def safe_segment(value):
    return (bool(value) and not value.startswith(".") and value == value.rstrip(". ")
            and not re.search(r'[\\:<>"/|?*\x00-\x1f\x7f]', value)
            and not re.fullmatch(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9]|LPT[1-9]", value.split(".")[0], re.I))


def plain(value):
    return "".join(c for c in value if not unicodedata.category(c).startswith("C"))[:1000].strip()


def bullets(value):
    return [item for part in value.split(";")[:30] if (item := plain(part))]


def archive_files(archive):
    entries = archive.infolist()
    require(0 < len(entries) <= 1000, "ZIP entry limit exceeded.")
    require(sum(entry.file_size for entry in entries) <= 64 * 1024 * 1024, "Expanded ZIP exceeds 64 MiB.")
    files, seen, roots, spellings = {}, set(), set(), {}
    for entry in entries:
        name = entry.orig_filename.rstrip("/") if entry.is_dir() else entry.orig_filename
        parts = name.split("/")
        require(len(name) <= 240 and len(parts) <= 20 and all(map(safe_segment, parts)), "Unsafe ZIP path.")
        roots.add(parts[0])
        key = unicodedata.normalize("NFC", name).casefold()
        require(key not in seen, "Duplicate ZIP path.")
        seen.add(key)
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            folded = unicodedata.normalize("NFC", prefix).casefold()
            require(spellings.setdefault(folded, prefix) == prefix, "Case or Unicode ZIP path collision.")
        mode = stat.S_IFMT(entry.external_attr >> 16)
        require(mode in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG), "ZIP contains a special file.")
        require(not (entry.flag_bits & 1) and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                "Unsupported ZIP compression or encryption.")
        if entry.is_dir():
            require(entry.file_size == 0, "Directory contains data.")
            continue
        require(len(parts) >= 2, "ZIP must contain one addon folder.")
        require(Path(name).suffix.lower() in SAFE_ASSETS, "Unsupported file type or nested archive.")
        require(entry.file_size <= max(1_048_576, entry.compress_size * 250), "Excessive compression ratio.")
        with archive.open(entry) as member:
            content = member.read(entry.file_size + 1)
        require(len(content) == entry.file_size, "Invalid ZIP member size.")
        require(not content.startswith((b"MZ", b"\x7fELF", b"PK\x03\x04", b"\x1bLua")), "Executable or nested archive detected.")
        files[name] = content
    file_keys = {unicodedata.normalize("NFC", name).casefold() for name in files}
    for name in seen:
        parts = name.split("/")
        require(all("/".join(parts[:i]) not in file_keys for i in range(1, len(parts))),
                "File and directory paths collide.")
    require(len(roots) == 1, "ZIP must contain exactly one addon folder.")
    return roots.pop(), files


def compile_lua(files, compiler="/usr/bin/luac5.1"):
    lua = [(name, data) for name, data in files.items() if name.lower().endswith(".lua")]
    with tempfile.TemporaryDirectory() as temp:
        paths = {}
        for index, (name, data) in enumerate(lua):
            path = Path(temp) / f"{index}.lua"
            path.write_bytes(data)
            paths[str(path)] = name
        deadline = time.monotonic() + 30
        for group in range(0, len(paths), 30):
            # Lua 5.1 parses each original chunk without executing it.
            time_left = deadline - time.monotonic()
            require(time_left > 0, "Lua compilation exceeded the package time limit.")
            try:
                result = subprocess.run([compiler, "-p", *list(paths)[group:group + 30]],
                                        cwd=temp, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                                        capture_output=True, timeout=min(time_left, 10))
            except subprocess.TimeoutExpired as exc:
                raise Rejected("Lua compilation timed out.") from exc
            if result.returncode:
                detail = result.stderr.decode(errors="replace")[:600]
                for path, name in paths.items():
                    detail = detail.replace(path, name)
                raise Rejected("Lua 5.1 compilation failed. " + detail)



MANAGER = "ShadyDesktopManager"
MANAGER_PREFIX = "releases/" + MANAGER + "/"


def manager_fields(fields):
    require(isinstance(fields, dict) and set(fields) == {"manager", "version", "url", "sha256"}
            and fields["manager"] == MANAGER, "Invalid manager manifest.")
    release = fields["version"]
    require(isinstance(release, str) and re.fullmatch(r"(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})", release),
            "Invalid manager version.")
    require(fields["url"] == f"ShadyAddonManager_{release}_windows_amd64.exe",
            "Invalid manager executable filename.")
    require(isinstance(fields["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", fields["sha256"]),
            "Invalid manager checksum.")
    return fields


def manager_archive(archive, selected):
    require(selected in ("auto", "stable"), "Manager updates use the stable channel.")
    entries = archive.infolist()
    require(len(entries) == 3, "Manager ZIP must contain exactly three files.")
    names = [entry.orig_filename for entry in entries]
    require(len(set(names)) == 3 and MANAGER_PREFIX + "latest.json" in names and "UPLOAD.txt" in names,
            "Invalid manager ZIP layout.")
    for entry in entries:
        require(not entry.is_dir() and stat.S_IFMT(entry.external_attr >> 16) in (0, stat.S_IFREG),
                "Manager ZIP contains a special file.")
        require(not (entry.flag_bits & 1) and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                "Unsupported manager ZIP compression or encryption.")
        limit = LIMIT if entry.orig_filename.endswith(".exe") else 16384
        require(0 < entry.file_size <= limit and entry.file_size <= max(1048576, entry.compress_size * 250),
                "Manager ZIP member size is invalid.")
    fields = manager_fields(json.loads(archive.read(MANAGER_PREFIX + "latest.json")))
    require(set(names) == {MANAGER_PREFIX + "latest.json", MANAGER_PREFIX + fields["url"], "UPLOAD.txt"},
            "Unexpected manager ZIP member.")
    executable = archive.read(MANAGER_PREFIX + fields["url"])
    require(digest(executable) == fields["sha256"], "Manager executable checksum mismatch.")
    require(len(executable) >= 64 and executable[:2] == b"MZ", "Invalid Windows executable.")
    offset = int.from_bytes(executable[60:64], "little")
    require(64 <= offset <= len(executable) - 26 and executable[offset:offset+6] == b"PE\x00\x00\x64\x86"
            and executable[offset+24:offset+26] == b"\x0b\x02", "Expected a Windows x64 PE executable.")
    archive.read("UPLOAD.txt")
    return fields, executable


def manager_plan(work, package, resume=False):
    folder = release_dir(work, MANAGER)
    fields = manager_fields(package["managerFields"])
    current = current_manifest(folder, "latest.json", fields, resume)
    incoming = version(fields["version"])[1]
    require(not current or matches_release(current, fields) or incoming > version(current["version"])[1],
            "Manager version must be newer than current.")
    target = folder / fields["url"]
    check_destination(work, folder, "latest.json", fields, current, resume)
    if folder.exists():
        for asset in folder.glob("ShadyAddonManager_*_windows_amd64.exe"):
            if resume and asset == target:
                continue
            old = asset.name[len("ShadyAddonManager_"):-len("_windows_amd64.exe")]
            try:
                channel, parts = version(old)
            except Rejected:
                continue
            require(incoming > parts, "Manager version must be newer than release history.")
    return folder, current


def inspect(data, selected="auto", compiler="/usr/bin/luac5.1"):
    require(0 < len(data) <= LIMIT, "ZIP exceeds 32 MiB or is empty.")
    require(data.startswith(b"PK\x03\x04"), "Expected a standard ZIP archive.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if MANAGER_PREFIX + "latest.json" in archive.namelist():
                fields, _ = manager_archive(archive, selected)
                return {"addon": MANAGER, "name": "Desktop Manager Update", "version": fields["version"],
                        "channel": "stable", "size": len(data), "sha256": digest(data), "url": fields["url"],
                        "managerFields": fields, "description": "", "features": [], "changelog": [], "warnings": []}
            addon, files = archive_files(archive)
    except (zipfile.BadZipFile, RuntimeError, UnicodeError, EOFError, OSError, json.JSONDecodeError) as exc:
        raise Rejected("ZIP structure or checksum is invalid.") from exc
    require(valid_id(addon), "Invalid addon ID.")
    main = f"{addon}/{addon}.toc"
    require(main in files, "Missing matching addon TOC.")
    metadata = {}
    for name, contents in files.items():
        if not name.lower().endswith(".toc"):
            continue
        require(len(contents) <= 128 * 1024, "TOC is too large.")
        fields = {}
        for line in contents.decode("utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("##"):
                label, separator, value = line[2:].partition(":")
                if separator:
                    label, value = label.strip().casefold(), value.strip()
                    if label in {"version", "x-release-channel", "x-shady-channel"}:
                        require(label not in fields or fields[label] == value, "Conflicting TOC metadata.")
                    fields[label] = value
            elif not line.startswith("#"):
                ref = line.replace("\\", "/")
                require(all(map(safe_segment, ref.split("/"))), "Unsafe TOC file reference.")
                require(f"{name.rsplit('/', 1)[0]}/{ref}" in files, f"Missing TOC reference: {plain(ref)}")
        metadata[name] = fields
    fields = metadata[main]
    release = fields.get("version", "")
    channel, _ = version(release)
    for other in metadata.values():
        require(other.get("version", release) == release, "TOCs disagree on version.")
        for label in ("x-release-channel", "x-shady-channel"):
            require(other.get(label, channel).casefold() == channel, "TOC channel conflicts with its version.")
    require(selected in ("auto", channel), "Selected channel conflicts with the TOC version.")
    changelog = bullets(fields.get("x-shady-changelog", ""))
    if not changelog:
        for name, contents in files.items():
            if name.count("/") == 1 and re.fullmatch(r"(?:change[ _-]?log|changes|release[ _-]?notes)\.(?:txt|md)",
                                                    name.split("/")[-1], re.I):
                changelog = [clean for line in contents[:8000].decode("utf-8-sig", errors="replace").splitlines()[:30]
                             if (clean := plain(line))]
                break
    compile_lua(files, compiler)
    return {"addon": addon, "version": release, "channel": channel, "size": len(data),
            "sha256": digest(data), "url": f"{addon}_v{release.replace(' ', '_')}.zip",
            "name": plain(fields.get("title", "")) or addon,
            "description": plain(fields.get("notes", "")),
            "features": bullets(fields.get("x-shady-features", "")),
            "changelog": changelog,
            "warnings": [] if fields.get("interface") else ["TOC has no Interface field."]}


def git(work, *args, token=None):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_COUNT": "3",
           "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull,
           "GIT_CONFIG_KEY_1": "http.followRedirects", "GIT_CONFIG_VALUE_1": "false",
           "GIT_CONFIG_KEY_2": "credential.helper", "GIT_CONFIG_VALUE_2": ""}
    if token:
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="4", GIT_CONFIG_KEY_3="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_3=f"Authorization: Basic {encoded}")
    result = subprocess.run(["git", "-C", str(work), *args], env=env, capture_output=True, timeout=90)
    if result.returncode:
        raise Rejected("Repository operation failed. Refresh and check history before retrying.")
    return result.stdout


@contextmanager
def checkout(remote=REPO, full=False):
    with tempfile.TemporaryDirectory() as temp:
        args = [] if full else ["--depth", "1"]
        git(temp, "clone", *args, "--single-branch", "--branch", "main", "--", remote, "repo")
        yield Path(temp) / "repo"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def release_dir(work, addon):
    require(addon == MANAGER or valid_id(addon), "Invalid addon ID.")
    root = work / "releases"
    folder = root / addon
    require(root.is_dir() and not root.is_symlink() and not folder.is_symlink(), "Unsafe repository layout.")
    require(not any(p.name.casefold() == addon.casefold() and p.name != addon for p in root.iterdir()),
            "Repository addon ID case mismatch.")
    return folder


def manifest(folder, filename, optional=True):
    path = folder / filename
    require(not path.is_symlink(), "Unsafe repository manifest.")
    if optional and not path.exists():
        return None
    return check_manifest(folder, read_json(path))


def check_manifest(folder, data):
    if folder.name == MANAGER:
        fields = manager_fields(data)
        asset = folder / fields["url"]
        require(not asset.is_symlink() and asset.is_file() and asset.stat().st_size <= LIMIT
                and digest(asset.read_bytes()) == fields["sha256"], "Published manager checksum mismatch.")
        return fields
    require(isinstance(data, dict) and data.get("addon") == folder.name, "Invalid current manifest.")
    version(data.get("version", ""))
    url = data.get("url", "")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.zip", url) and ".." not in url,
            "Invalid release filename.")
    asset = folder / url
    require(not asset.is_symlink() and asset.is_file() and asset.stat().st_size <= LIMIT
            and digest(asset.read_bytes()) == data.get("sha256"), "Published ZIP checksum does not match its manifest.")
    return data


def release_reference(package):
    if package["addon"] == MANAGER:
        return manager_fields(package["managerFields"])
    return {key: package[key] for key in ("addon", "version", "url", "sha256")}


def matches_release(data, fields):
    return isinstance(data, dict) and all(data.get(key) == value for key, value in fields.items())


def read_release_metadata(folder, filename):
    path = folder / filename
    require(not path.is_symlink(), "Unsafe repository manifest.")
    if not path.exists():
        return None
    data = read_json(path)
    require(isinstance(data, dict), "Invalid repository manifest.")
    return data


def current_manifest(folder, filename, fields, resume):
    data = read_release_metadata(folder, filename)
    if data is None or (resume and matches_release(data, fields)):
        return data
    return check_manifest(folder, data)


def asset_state(folder, fields):
    path = folder / fields["url"]
    if not path.exists() and not path.is_symlink():
        return "missing"
    if (not path.is_symlink() and path.is_file() and path.stat().st_size <= LIMIT
            and digest(path.read_bytes()) == fields["sha256"]):
        return "matching"
    return "conflict"


def catalog_entries(work):
    catalog_path = work / "catalog.json"
    require(not catalog_path.is_symlink(), "Unsafe catalog path.")
    catalog = read_json(catalog_path)
    require(isinstance(catalog, list), "Invalid catalog.")
    ids = [row["id"] for row in catalog]
    require(all(map(valid_id, ids)) and len({x.casefold() for x in ids}) == len(ids), "Invalid catalog IDs.")
    return catalog


def publication_state(work, fields, filename):
    addon = fields.get("addon", fields.get("manager"))
    folder = release_dir(work, addon)
    live = read_release_metadata(folder, filename)
    asset = asset_state(folder, fields)
    catalog = addon == MANAGER or any(row["id"] == addon for row in catalog_entries(work))
    return {"asset": asset, "metadata": matches_release(live, fields), "catalog": catalog, "live": live}


def check_destination(work, folder, filename, fields, current, resume):
    state = asset_state(folder, fields) if resume else None
    target = folder / fields["url"]
    if not target.exists() and not target.is_symlink():
        return
    require(resume and state == "matching", "This versioned file already exists with different or unverified bytes.")
    if not matches_release(current, fields):
        # A retained file from a superseded or rolled back release is never promoted by a publish retry.
        if git(work, "rev-parse", "--is-shallow-repository").strip() == b"true":
            git(work, "fetch", "--unshallow", "origin", "main")
        history = git(work, "log", "-1", "-S", fields["url"], "--", str((folder / filename).relative_to(work)))
        require(not history, "This version was previously published. Use release history to restore it.")


def plan(work, package, resume=False):
    addon, channel = package["addon"], package["channel"]
    folder = release_dir(work, addon)
    filename = CHANNELS[channel]
    fields = release_reference(package)
    latest = current_manifest(folder, "latest.json", fields, resume)
    current = latest if channel == "stable" else current_manifest(folder, filename, fields, resume)
    if current and channel != "stable":
        require(version(current["version"])[0] == channel, "Existing prerelease manifest has a channel mismatch.")
    candidates = []
    if current and not (resume and matches_release(current, fields)) and version(current["version"])[0] == channel:
        candidates.append(version(current["version"])[1])
    if latest and not (resume and matches_release(latest, fields)) and version(latest["version"])[0] == channel:
        candidates.append(version(latest["version"])[1])
    target = folder / package["url"]
    check_destination(work, folder, filename, fields, current, resume)
    if folder.exists():
        for asset in folder.glob(f"{addon}_v*.zip"):
            if resume and asset == target:
                continue
            try:
                old_channel, parts = version(asset.name[len(addon) + 2:-4].replace("_", " "))
            except Rejected:
                continue
            if old_channel == channel:
                candidates.append(parts)
    incoming = version(package["version"])[1]
    if not (resume and matches_release(current, fields)):
        require(incoming not in candidates, "This version already exists in the channel.")
        require(not candidates or incoming > max(candidates), "Publishing would downgrade this channel.")
    catalog = catalog_entries(work)
    ids = [row["id"] for row in catalog]
    require(addon in ids or addon.casefold() not in {x.casefold() for x in ids}, "Catalog ID case mismatch.")
    return folder, filename, latest, current, catalog


def preview(work, package, resume=False):
    if package["addon"] == MANAGER:
        _, current = manager_plan(work, package, resume)
        live = current["version"] if current else None
        return {**package, "current": live, "publicStable": live,
                "destination": MANAGER_PREFIX + "latest.json", "catalogChange": False}
    _, filename, latest, current, catalog = plan(work, package, resume)
    addon = package["addon"]
    return {**package, "current": current["version"] if current else None,
            "publicStable": latest["version"] if latest and version(latest["version"])[0] == "stable" else None,
            "destination": f"releases/{addon}/{filename}",
            "catalogChange": not any(row["id"] == addon for row in catalog)
                             or bool(package["channel"] == "stable" and package["features"])}


def apply(work, package, data, resume=False):
    if package["addon"] == MANAGER:
        folder, current = manager_plan(work, package, resume)
        require(digest(data) == package["sha256"], "Manager ZIP changed after validation.")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            fields, executable = manager_archive(archive, "stable")
        require(fields == package["managerFields"], "Manager manifest changed.")
        folder.mkdir(exist_ok=True)
        paths = write_release(folder, "latest.json", fields, executable, current, resume)
        return fields, paths
    folder, filename, latest, current, catalog = plan(work, package, resume)
    folder.mkdir(exist_ok=True)
    addon, channel = package["addon"], package["channel"]
    paths = []
    if channel == "stable" and latest:
        legacy = version(latest["version"])[0]
        if legacy != "stable" and not manifest(folder, CHANNELS[legacy]):
            backup = folder / CHANNELS[legacy]
            backup.write_bytes((folder / "latest.json").read_bytes())
            paths.append(backup)
    existing = next((row for row in catalog if row["id"] == addon), None)
    features = package["features"] if channel == "stable" else []
    if existing is None:
        catalog.append({"id": addon, "name": package["name"], "category": "Utility",
                        "description": package["description"] or "WoW Forever addon.", "features": features})
    elif features:
        existing["features"] = features
        if package["description"]:
            existing["description"] = package["description"]
    if existing is None or (features and catalog != read_json(work / "catalog.json")):
        catalog_path = work / "catalog.json"
        write_json(catalog_path, catalog)
        paths.append(catalog_path)
    fields = release_reference(package)
    if resume and matches_release(current, fields):
        fields = current
    else:
        fields["published_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if package["changelog"]:
            fields["changelog"] = package["changelog"]
    paths.extend(write_release(folder, filename, fields, data, current, resume))
    return fields, paths


def write_release(folder, filename, fields, data, current, resume):
    paths = []
    target = folder / fields["url"]
    if not resume or not target.exists():
        target.write_bytes(data)
        paths.append(target)
    if not resume or fields != current:
        path = folder / filename
        write_json(path, fields)
        paths.append(path)
    return paths


def commit_push(work, paths, message, token=None):
    base = git(work, "rev-parse", "HEAD").strip()
    current = git(work, "ls-remote", "origin", "refs/heads/main").split()
    require(current and current[0] == base, "Repository changed. Validate again before retrying.")
    git(work, "add", "-f", "--", *(str(path.relative_to(work)) for path in paths))
    git(work, "-c", "user.name=Shady Remote Publisher", "-c", "user.email=remote-publisher@users.noreply.github.com",
        "commit", "-m", message)
    sha = git(work, "rev-parse", "HEAD").decode().strip()
    try:
        git(work, "push", "origin", "HEAD:refs/heads/main", token=token)
    except (Rejected, OSError, subprocess.SubprocessError) as exc:
        LOGGER.warning("Git push response unconfirmed for %s (%s)", sha, type(exc).__name__)
        return sha, False
    return sha, True


def verify_public(addon, filename, fields, sha, *, catalog_required=False):
    manifest_path = f"releases/{addon}/{filename}"
    asset_path = f"releases/{addon}/{quote(fields['url'])}"
    stamp = f"?remote={sha}-{os.urandom(5).hex()}"
    sources = (("GitHub commit", f"{RAW}/{sha}"), ("GitHub main", f"{RAW}/main"), ("GitHub Pages", PAGES))
    for source, address in sources:
        try:
            with urlopen(f"{address}/{manifest_path}{stamp}", timeout=8) as response:
                current = json.loads(response.read(1024 * 1024))
            require(current == fields, "version metadata differs")
            with urlopen(f"{address}/{asset_path}{stamp}", timeout=8) as response:
                require(digest(response.read(LIMIT + 1)) == fields["sha256"], "download checksum differs")
            if address == PAGES and addon != MANAGER and (catalog_required or filename == "latest.json"):
                with urlopen(f"{address}/catalog.json{stamp}", timeout=8) as response:
                    catalog = json.loads(response.read(1024 * 1024))
                require(any(row["id"] == addon for row in catalog), "addon is missing from the public catalog")
        except Rejected as exc:
            raise Rejected(f"{source}: {exc}.") from exc
        except CHECK_ERRORS as exc:
            code = getattr(exc, "code", None)
            detail = f"HTTP {code}" if isinstance(code, int) else type(exc).__name__
            raise Rejected(f"{source} could not be checked ({detail}).") from exc
    return f"{PAGES}/{asset_path}"
