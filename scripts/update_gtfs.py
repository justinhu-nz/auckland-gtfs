#!/usr/bin/env python3
"""Refresh AT's static GTFS feed; optionally archive snapshots as GitHub releases."""

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
import zipfile

SOURCE = "https://gtfs.at.govt.nz/gtfs.zip"
ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "agency.txt": {"agency_name", "agency_url", "agency_timezone"},
    "stops.txt": {"stop_id", "stop_name", "stop_lat", "stop_lon"},
    "routes.txt": {"route_id", "route_type"},
    "trips.txt": {"route_id", "service_id", "trip_id"},
    "stop_times.txt": {"trip_id", "stop_id", "stop_sequence"},
    "feed_info.txt": {"feed_publisher_name", "feed_version", "feed_start_date", "feed_end_date"},
}


def today():
    return datetime.now(ZoneInfo("Pacific/Auckland")).date()


def inspect_feed(path, require_current=True):
    """Validate archive/CSV structure and hash contents independently of ZIP metadata."""
    files, counts, feed = {}, {}, None
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate ZIP entries")
        if not REQUIRED.keys() <= set(names):
            raise ValueError(f"Missing required files: {REQUIRED.keys() - set(names)}")
        if not {"calendar.txt", "calendar_dates.txt"} & set(names):
            raise ValueError("Missing service calendar")
        if sum(entry.file_size for entry in archive.infolist()) > 2_000_000_000:
            raise ValueError("Feed exceeds 2 GB uncompressed limit")
        for name in sorted(names):
            if Path(name).name != name or not name.endswith(".txt") or "\\" in name:
                raise ValueError(f"Unexpected ZIP entry: {name}")
            with archive.open(name) as member:
                files[name] = hashlib.file_digest(member, "sha256").hexdigest()
            with archive.open(name) as member:
                rows = csv.DictReader(io.TextIOWrapper(member, encoding="utf-8-sig", newline=""), strict=True)
                headers = rows.fieldnames
                if not headers or len(headers) != len(set(headers)):
                    raise ValueError(f"Invalid CSV headers: {name}")
                if not REQUIRED.get(name, set()) <= set(headers):
                    raise ValueError(f"Missing required columns: {name}")
                count = 0
                for row in rows:
                    if None in row or None in row.values():
                        raise ValueError(f"Malformed CSV row in {name}, row {count + 2}")
                    count += 1
                    if name == "feed_info.txt":
                        feed = row
                counts[name] = count
                if name in REQUIRED and count == 0:
                    raise ValueError(f"Empty required file: {name}")
    if counts["feed_info.txt"] != 1:
        raise ValueError("Expected one feed_info row")
    start = datetime.strptime(feed["feed_start_date"], "%Y%m%d").date()
    end = datetime.strptime(feed["feed_end_date"], "%Y%m%d").date()
    if start > end:
        raise ValueError("Feed validity dates are reversed")
    if require_current and end < today():
        raise ValueError(f"Downloaded feed expired on {end}")
    content_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    with path.open("rb") as source:
        zip_hash = hashlib.file_digest(source, "sha256").hexdigest()
    return {"content_sha256": content_hash, "zip_sha256": zip_hash,
            "feed_version": feed["feed_version"], "valid_from": str(start),
            "valid_until": str(end), "counts": counts, "files": files}


def download(destination):
    for attempt in range(3):
        try:
            request = Request(SOURCE, headers={"User-Agent": "auckland-gtfs-mirror/1.0"})
            with urlopen(request, timeout=120) as response, destination.open("wb") as output:
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 128_000_000:
                        raise ValueError("Download exceeds 128 MB limit")
                    output.write(chunk)
            return
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


class GitHub:
    def __init__(self):
        self.token = os.environ["GH_TOKEN"]
        self.repository = os.environ["GITHUB_REPOSITORY"]
        self.base = f"https://api.github.com/repos/{self.repository}/releases"

    def request(self, url, method="GET", data=None, content_type="application/json"):
        if isinstance(data, dict):
            data = json.dumps(data).encode()
        request = Request(url, method=method, data=data, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "Content-Type": content_type,
            "User-Agent": "auckland-gtfs-mirror/1.0",
        })
        with urlopen(request, timeout=180) as response:
            body = response.read()
            return json.loads(body) if body else None

    def archive(self, path, info, initial=False):
        tag = "gtfs-" + info["content_sha256"]
        try:
            release = self.request(f"{self.base}/tags/{tag}")
        except HTTPError as error:
            if error.code != 404:
                raise
            # Drafts are not returned by the release-by-tag endpoint. Find a draft
            # left by a failed upload so a rerun can finish it rather than duplicate it.
            release = None
            page = 1
            while True:
                batch = self.request(f"{self.base}?per_page=100&page={page}")
                release = next((r for r in batch if r["tag_name"] == tag), None)
                if release or len(batch) < 100:
                    break
                page += 1
            if release is None:
                note = "Initial repository snapshot; original download date unknown." if initial else "Downloaded from Auckland Transport."
                body = (f"{note}\n\nArchived: {today()} (Pacific/Auckland)\n"
                        f"Source: {SOURCE}\nFeed version: `{info['feed_version']}`\n"
                        f"Valid: {info['valid_from']} to {info['valid_until']}\n\n"
                        f"ZIP SHA-256: `{info['zip_sha256']}`\n"
                        f"Content SHA-256: `{info['content_sha256']}`\n\n"
                        "See metadata.json for file hashes and row counts.")
                release = self.request(self.base, "POST", {
                    "tag_name": tag, "target_commitish": os.environ["GITHUB_SHA"],
                    "name": f"GTFS {today()} · {info['content_sha256'][:12]}",
                    "body": body, "draft": True,
                })
        assets = {a["name"]: a for a in release["assets"]}
        payloads = {"gtfs.zip": lambda: path.read_bytes(),
                    "metadata.json": lambda: (json.dumps({**info, "source": SOURCE,
                        "archived_on": str(today()), "initial_snapshot": initial}, indent=2) + "\n").encode()}
        upload_url = release["upload_url"].split("{")[0]
        for name, payload in payloads.items():
            asset = assets.get(name)
            if asset and asset["state"] == "uploaded" and asset["size"] > 0:
                continue
            if asset:
                self.request(asset["url"], "DELETE")
            data = payload()
            uploaded = self.request(f"{upload_url}?name={name}", "POST", data,
                                    "application/zip" if name.endswith(".zip") else "application/json")
            if uploaded.get("state") != "uploaded" or uploaded.get("size") != len(data):
                raise RuntimeError(f"Incomplete release upload: {name}")
        if release["draft"]:
            self.request(release["url"], "PATCH", {"draft": False, "make_latest": "false"})
        print(f"Archived: https://github.com/{self.repository}/releases/tag/{tag}")


def update(root=ROOT, dry_run=False, archive=False):
    # Stage on the same filesystem so replacements use rename, and discard the
    # download automatically if validation or release publication fails.
    with tempfile.TemporaryDirectory(prefix=".gtfs-update-", dir=root) as directory:
        stage = Path(directory)
        incoming = stage / "gtfs.zip"
        download(incoming)
        new = inspect_feed(incoming)
        current = root / "gtfs.zip"
        old = inspect_feed(current, require_current=False) if current.exists() else None
        changed = old is None or old["content_sha256"] != new["content_sha256"]
        # Also repair an extracted directory that has drifted from the ZIP.
        extracted = root / "gtfs"
        if not changed:
            actual = {}
            if extracted.exists():
                for path in extracted.rglob("*"):
                    if path.is_file():
                        with path.open("rb") as source:
                            actual[str(path.relative_to(extracted))] = hashlib.file_digest(source, "sha256").hexdigest()
            changed = actual != new["files"]
        summary = (f"Feed {new['feed_version']}\nValid: {new['valid_from']} to {new['valid_until']}\n"
                   f"Routes: {new['counts']['routes.txt']}; stops: {new['counts']['stops.txt']}; "
                   f"trips: {new['counts']['trips.txt']}\nChanged: {str(changed).lower()}\n")
        print(summary)
        if dry_run:
            return changed
        if archive:
            github = GitHub()
            if old:
                github.archive(current, old, initial=True)
            if old is None or old["content_sha256"] != new["content_sha256"]:
                github.archive(incoming, new)
        if changed:
            staged_files = stage / "gtfs"
            staged_files.mkdir()
            with zipfile.ZipFile(incoming) as bundle:
                bundle.extractall(staged_files)
            backup = stage / "previous-gtfs"
            if extracted.exists():
                extracted.rename(backup)
            try:
                staged_files.rename(extracted)
                incoming.replace(current)
            except OSError:
                if extracted.exists():
                    shutil.rmtree(extracted)
                if backup.exists():
                    backup.rename(extracted)
                raise
        for variable, value in (("GITHUB_OUTPUT", f"changed={str(changed).lower()}\n"),
                                ("GITHUB_STEP_SUMMARY", "### Auckland GTFS\n\n```text\n" + summary + "```\n")):
            if target := os.environ.get(variable):
                with open(target, "a") as output:
                    output.write(value)
        return changed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Download and validate without changing files or releases")
    parser.add_argument("--archive", action="store_true", help="Archive before updating (requires GH_TOKEN, GITHUB_REPOSITORY, GITHUB_SHA)")
    args = parser.parse_args()
    update(dry_run=args.dry_run, archive=args.archive)
