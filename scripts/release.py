#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign existing build outputs and optionally upload a draft; never promote."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
BOARDS = {"q703": "fujitsu,q703", "ts221": "qnap,ts221"}


def manifest(dist, repo, tag, sequence, board):
    compatible = BOARDS[board]
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("GITHUB_REPOSITORY must be OWNER/REPO")
    if not re.fullmatch(r"v[0-9][A-Za-z0-9._-]{0,63}", tag):
        raise ValueError("tag must start with v and a digit; use letters, digits, ._- only")
    source = json.loads((dist / "sources.json").read_text())
    commit = source.get("project_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        raise ValueError("resolve/build with GITHUB_SHA set to the source commit before signing")
    if not (dist / "linux.tar.gz").is_file():
        raise ValueError("all three builds must complete before signing")
    files = {}
    for name, limit in ((f"openwrt-{board}.tar.gz", 256 * 1024 * 1024),
                        (f"u-boot-{board}.kwb", 512 * 1024)):
        data = (dist / name).read_bytes()
        if not 0 < len(data) <= limit:
            raise ValueError(f"invalid size: {name}")
        files[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if type(sequence) is not int or not 0 < sequence < 10**10:
        raise ValueError("sequence must be a positive integer of at most ten digits")
    return {"schema": 2, "layout": 1, "board": compatible, "repository": repo, "tag": tag, "sequence": sequence,
            "project_commit": commit, "files": files}


def asset_names():
    assets = ["linux.tar.gz", "sources.json", "release.pub"]
    for board in BOARDS:
        assets.extend((f"openwrt-{board}.tar.gz", f"u-boot-{board}.kwb",
                       f"u-boot-{board}.config", f"manifest-{board}.json",
                       f"manifest-{board}.sig"))
    return assets


def sign(dist, repo, tag, key):
    sequence = int(time.time())
    for board in BOARDS:
        info = manifest(dist, repo, tag, sequence, board)
        output = dist / f"manifest-{board}.json"
        signature = output.with_suffix(".sig")
        output.write_text(json.dumps(info, sort_keys=True, indent=2) + "\n")
        subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", key,
                        "-in", str(output), "-out", str(signature)], check=True)
        if signature.stat().st_size != 64:
            raise ValueError("RELEASE_KEY must be Ed25519")
        subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-rawin",
                        "-inkey", str(dist / "release.pub"), "-in", str(output),
                        "-sigfile", str(signature)], check=True)
    return info["project_commit"]


def verify_signed(dist, repo, tag):
    sequence = None
    project_commit = None
    for board in BOARDS:
        output = dist / f"manifest-{board}.json"
        signature = output.with_suffix(".sig")
        info = json.loads(output.read_text())
        expected = manifest(dist, repo, tag, info.get("sequence"), board)
        if info != expected:
            raise ValueError(f"stale or inconsistent manifest: {output.name}")
        subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-rawin",
                        "-inkey", str(dist / "release.pub"), "-in", str(output),
                        "-sigfile", str(signature)], check=True)
        if sequence is None:
            sequence = info["sequence"]
            project_commit = info["project_commit"]
        elif sequence != info["sequence"]:
            raise ValueError("model manifests have different release sequences")
    for name in asset_names():
        if not (dist / name).is_file():
            raise ValueError(f"missing release asset: {name}")
    return project_commit


def upload_draft(dist, repo, tag, project_commit):
    subprocess.run(["gh", "release", "create", tag, "--repo", repo,
                    "--target", project_commit, "--draft", "--title", tag,
                    "--notes", "Evaluation build. Qualify these exact artifacts before promotion.",
                    *[str(dist / name) for name in asset_names()]], check=True)


def main():
    args = sys.argv[1:]
    if len(args) == 1:
        mode, tag = "all", args[0]
    elif len(args) == 2 and args[0] in {"sign", "verify", "draft"}:
        mode, tag = args
    else:
        raise SystemExit(
            "usage: pixi run release [sign|verify|draft] vYYYY.MM.DD.N"
        )

    dist = ROOT / "dist"
    repo = os.environ["GITHUB_REPOSITORY"]
    if mode in {"all", "sign"}:
        project_commit = sign(dist, repo, tag, os.environ["RELEASE_KEY"])
    else:
        project_commit = verify_signed(dist, repo, tag)
    if mode in {"all", "draft"}:
        upload_draft(dist, repo, tag, project_commit)


if __name__ == "__main__":
    main()
