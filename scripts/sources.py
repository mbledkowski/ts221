#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve stable releases once; fetch only the resulting lock (Python 3.12+)."""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
from urllib.parse import quote, urlparse
from urllib.error import URLError
from urllib.request import Request, urlopen

WORK = Path(__file__).resolve().parents[1] / "work"
LOCK = WORK / "sources.json"


class SourceNetworkError(Exception):
    """An upstream request failed before a source could be resolved or fetched."""


def request(url):
    if urlparse(url).scheme != "https":
        raise ValueError(f"HTTPS required: {url}")
    headers = {"User-Agent": "firmware-build"}
    if urlparse(url).hostname == "api.github.com" and os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    try:
        return urlopen(Request(url, headers=headers), timeout=120)
    except URLError as error:
        raise SourceNetworkError(
            f"Cannot fetch {url}: {error.reason}. "
            "Check DNS/network connectivity and upstream availability, then retry."
        ) from None


def read(url):
    with request(url) as response:
        return response.read().decode()


def latest(versions):
    return max((v for v in versions if re.fullmatch(r"\d+(?:\.\d+)+", v)),
               key=lambda v: tuple(map(int, v.split("."))))


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def download(url, expected=None):
    if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Invalid archive SHA256")
    cache = WORK / "archives"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (expected or hashlib.sha256(url.encode()).hexdigest())
    if not path.exists():
        partial = path.with_suffix(".part")
        try:
            with request(url) as response, partial.open("wb") as target:
                shutil.copyfileobj(response, target)
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
    actual = digest(path)
    if expected and actual != expected:
        raise ValueError(f"Archive checksum mismatch: {path}")
    final = cache / actual
    if path != final:
        path.replace(final)
    return final


def pin(repo, ref, version=None, url=None):
    data = json.loads(read(f"https://api.github.com/repos/{repo}/commits/{quote(ref, safe='')}"))
    commit = data["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid upstream commit")
    if re.fullmatch(r"[0-9a-f]{40}", ref) and commit != ref:
        raise ValueError("Upstream did not return the pinned commit")
    url = url or f"https://codeload.github.com/{repo}/tar.gz/{commit}"
    return {
        "version": version or commit,
        "commit": commit,
        "epoch": int(datetime.fromisoformat(data["commit"]["committer"]["date"]).timestamp()),
        "url": url,
        "sha256": download(url).name,
    }


def kernel(release):
    version = release["version"]
    source = release["source"]
    epoch = release["released"]["timestamp"]
    if not re.fullmatch(r"\d+(?:\.\d+)+", version) or not isinstance(epoch, int) or epoch <= 0:
        raise ValueError("Invalid stable Linux release")
    return {
        "version": version,
        "epoch": epoch,
        "url": source,
        "sha256": download(source).name,
    }


def feeds(text):
    result = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        kind, name, source = line.split()
        if kind not in ("src-git", "src-git-full") or not re.fullmatch(r"[a-z0-9_-]+", name):
            raise ValueError(f"Unsupported feed: {line}")
        match = re.fullmatch(r"https://(?:git\.openwrt\.org/(?:feed|project)/|github\.com/openwrt/)([a-z0-9_-]+)\.git([;^])(.+)", source)
        if not match or name in result:
            raise ValueError(f"Unsupported or duplicate feed: {line}")
        repo, separator, ref = match.groups()
        if separator == "^" and not re.fullmatch(r"[0-9a-f]{40}", ref):
            raise ValueError(f"Invalid feed commit: {line}")
        result[name] = pin(f"openwrt/{repo}", ref)
    if not result:
        raise ValueError("No OpenWrt feeds found")
    return result


def resolve():
    project_commit = os.getenv("GITHUB_SHA")
    if project_commit and not re.fullmatch(r"[0-9a-f]{40}", project_commit):
        raise ValueError("Invalid GITHUB_SHA")
    uboot = latest(re.findall(r'href="u-boot-([^"]+)\.tar\.bz2"', read("https://ftp.denx.de/pub/u-boot/")))
    openwrt = latest(re.findall(r'href="([^"/]+)/"', read("https://downloads.openwrt.org/releases/")))
    releases = json.loads(read("https://www.kernel.org/releases.json"))
    linux = releases["latest_stable"]["version"]
    if latest([linux]) != linux:
        raise ValueError("Invalid latest stable Linux version")
    linux_release = next(r for r in releases["releases"] if r["version"] == linux and not r["iseol"])
    lock = {
        "u-boot": pin("u-boot/u-boot", f"v{uboot}", uboot,
                      f"https://ftp.denx.de/pub/u-boot/u-boot-{uboot}.tar.bz2"),
        "openwrt": pin("openwrt/openwrt", f"v{openwrt}", openwrt),
        "linux": kernel(linux_release),
    }
    commit = lock["openwrt"]["commit"]
    lock["openwrt"]["feeds"] = feeds(read(f"https://raw.githubusercontent.com/openwrt/openwrt/{commit}/feeds.conf.default"))
    if project_commit:
        lock["project_commit"] = project_commit
    WORK.mkdir(parents=True, exist_ok=True)
    temporary = LOCK.with_suffix(".tmp")
    temporary.write_text(json.dumps(lock, indent=2) + "\n")
    temporary.replace(LOCK)
    return lock


def extract(source, destination):
    marker = destination / ".source"
    if destination.exists():
        if marker.is_file() and marker.read_text().strip() == source["sha256"]:
            return
        refresh(source, destination)
        return
    archive = download(source["url"], source["sha256"])
    with tempfile.TemporaryDirectory(dir=WORK) as temporary, tarfile.open(archive) as tar:
        tar.extractall(temporary, filter="data")
        roots = list(Path(temporary).iterdir())
        if len(roots) != 1 or not roots[0].is_dir() or roots[0].is_symlink():
            raise ValueError("Archive must contain one source directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        roots[0].rename(destination)
    marker.write_text(source["sha256"] + "\n")


def refresh(source, destination):
    """Re-extract one verified generated source tree without leaving a backup."""
    expected = source["sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Invalid source SHA256")
    if destination.parent != WORK or not re.fullmatch(r"[a-z0-9-]+", destination.name):
        raise ValueError(f"Refusing unsafe source refresh: {destination}")

    sentinel = WORK / f".{destination.name}.refreshing"
    if sentinel.exists():
        if (not sentinel.is_file() or sentinel.is_symlink()
                or sentinel.read_text().strip() != expected):
            raise ValueError(f"Mismatched source refresh sentinel: {sentinel}")
    else:
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError(f"Refusing to refresh unknown source: {destination}")
        marker = destination / ".source"
        if not marker.is_file() or marker.is_symlink():
            raise ValueError(f"Refusing to refresh unverified source: {destination}")
        previous = marker.read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", previous):
            raise ValueError(f"Refusing invalid source marker: {destination}")
        if previous != expected:
            previous_archive = WORK / "archives" / previous
            if (not previous_archive.is_file() or previous_archive.is_symlink()
                    or digest(previous_archive) != previous):
                raise ValueError(
                    f"Refusing to replace source without its verified archive: {destination}"
                )
        # Verify the cached/downloaded archive before authorizing removal.
        download(source["url"], expected)
        sentinel.write_text(expected + "\n")

    if destination.is_symlink():
        raise ValueError(f"Refusing to refresh symlinked source: {destination}")
    if destination.exists():
        shutil.rmtree(destination)
    extract(source, destination)
    sentinel.unlink()


def refresh_component(name):
    lock = json.loads(LOCK.read_text())
    refresh(lock[name], WORK / name)


def fetch(name):
    lock = json.loads(LOCK.read_text())
    destination = WORK / name
    extract(lock[name], destination)
    if name == "openwrt":
        entries = []
        for feed, source in lock[name]["feeds"].items():
            if not re.fullmatch(r"[a-z0-9_-]+", feed):
                raise ValueError("Invalid feed name")
            target = WORK / "feeds" / feed
            extract(source, target)
            entries.append(f"src-link {feed} ../../feeds/{feed}")
        (destination / "feeds.conf").write_text("\n".join(entries) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("resolve", "fetch", "refresh"))
    parser.add_argument("component", nargs="?", choices=("u-boot", "openwrt", "linux"))
    args = parser.parse_args()
    if args.command in ("fetch", "refresh") and not args.component:
        parser.error(f"{args.command} requires a component")
    if args.command == "resolve" and args.component:
        parser.error("resolve takes no component")
    try:
        if args.command == "resolve":
            resolve()
        elif args.command == "fetch":
            fetch(args.component)
        else:
            refresh_component(args.component)
    except SourceNetworkError as error:
        parser.exit(1, f"STOP: {error}\n")
