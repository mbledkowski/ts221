# SPDX-License-Identifier: AGPL-3.0-or-later
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("release", Path(__file__).parents[1] / "scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dist = Path(self.tmp.name) / "dist"
        self.dist.mkdir()
        (self.dist / "sources.json").write_text(json.dumps({"project_commit": "a" * 40}))
        (self.dist / "linux.tar.gz").write_bytes(b"test artifact")
        for board in release.BOARDS:
            for name in (f"openwrt-{board}.tar.gz", f"u-boot-{board}.kwb", f"u-boot-{board}.config"):
                (self.dist / name).write_bytes(b"test artifact")

    def test_device_manifest_excludes_standalone_linux(self):
        for board, compatible in release.BOARDS.items():
            with self.subTest(board=board):
                result = release.manifest(self.dist, "owner/firmware", "v2026.09.05.1", 123, board)
                self.assertEqual((result["schema"], result["layout"]), (2, 1))
                self.assertEqual(result["board"], compatible)
                self.assertEqual(set(result["files"]), {f"openwrt-{board}.tar.gz", f"u-boot-{board}.kwb"})
                self.assertEqual(result["files"][f"u-boot-{board}.kwb"]["size"], 13)

    def test_invalid_identity(self):
        for repo, tag in [("../other/repo", "v1"), ("owner/repo", "v1/../../escape")]:
            with self.assertRaises(ValueError):
                release.manifest(self.dist, repo, tag, 123, "q703")

    def test_invalid_sequence(self):
        for sequence in (0, -1, 10**10, "123", True):
            with self.subTest(sequence=sequence), self.assertRaises(ValueError):
                release.manifest(self.dist, "owner/firmware", "v1", sequence, "q703")

    def test_unbound_source(self):
        (self.dist / "sources.json").write_text("{}")
        with self.assertRaises(ValueError):
            release.manifest(self.dist, "owner/repo", "v1", 123, "q703")

    def test_missing_build(self):
        (self.dist / "linux.tar.gz").unlink()
        with self.assertRaises(ValueError):
            release.manifest(self.dist, "owner/repo", "v1", 123, "q703")

    def test_oversized_bootloader(self):
        (self.dist / "u-boot-q703.kwb").write_bytes(b"x" * (512 * 1024 + 1))
        with self.assertRaises(ValueError):
            release.manifest(self.dist, "owner/repo", "v1", 123, "q703")

    def test_signing_key_must_match_image_and_upload_stays_draft(self):
        key = Path(self.tmp.name) / "key"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)], check=True)
        subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(self.dist / "release.pub")], check=True)
        calls = []
        run = subprocess.run

        def command(args, **kwargs):
            if args[0] == "gh":
                calls.append(args)
                return subprocess.CompletedProcess(args, 0)
            return run(args, capture_output=True, **kwargs)

        with patch.object(release, "ROOT", Path(self.tmp.name)), \
                patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/firmware", "RELEASE_KEY": str(key)}), \
                patch.object(sys, "argv", ["release.py", "v1"]), \
                patch.object(release.subprocess, "run", side_effect=command):
            release.main()
            self.assertEqual(len(calls), 1)
            self.assertIn("--draft", calls[0])
            self.assertIn("a" * 40, calls[0])
            manifests = []
            for board in release.BOARDS:
                for name in (f"manifest-{board}.json", f"manifest-{board}.sig",
                             f"openwrt-{board}.tar.gz", f"u-boot-{board}.kwb", f"u-boot-{board}.config"):
                    self.assertIn(str(self.dist / name), calls[0])
                manifests.append(json.loads((self.dist / f"manifest-{board}.json").read_text()))
            self.assertEqual(manifests[0]["sequence"], manifests[1]["sequence"])
            self.assertIn(str(self.dist / "linux.tar.gz"), calls[0])
            run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)], check=True)
            with self.assertRaises(subprocess.CalledProcessError):
                release.main()
            self.assertEqual(len(calls), 1)

    def test_local_signing_does_not_contact_github(self):
        key = Path(self.tmp.name) / "key"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
            check=True,
        )
        subprocess.run(
            [
                "openssl",
                "pkey",
                "-in",
                str(key),
                "-pubout",
                "-out",
                str(self.dist / "release.pub"),
            ],
            check=True,
        )
        calls = []
        run = subprocess.run

        def command(args, **kwargs):
            if args[0] == "gh":
                calls.append(args)
                return subprocess.CompletedProcess(args, 0)
            return run(args, capture_output=True, **kwargs)

        with (
            patch.object(release, "ROOT", Path(self.tmp.name)),
            patch.dict(
                os.environ,
                {
                    "GITHUB_REPOSITORY": "owner/firmware",
                    "RELEASE_KEY": str(key),
                },
            ),
            patch.object(sys, "argv", ["release.py", "sign", "v1"]),
            patch.object(release.subprocess, "run", side_effect=command),
        ):
            release.main()

        self.assertEqual(calls, [])
        for board in release.BOARDS:
            self.assertTrue((self.dist / f"manifest-{board}.json").is_file())
            self.assertTrue((self.dist / f"manifest-{board}.sig").is_file())

        with (
            patch.object(release, "ROOT", Path(self.tmp.name)),
            patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/firmware"}),
            patch.object(sys, "argv", ["release.py", "verify", "v1"]),
            patch.object(release.subprocess, "run", side_effect=command),
        ):
            os.environ.pop("RELEASE_KEY", None)
            release.main()
        self.assertEqual(calls, [])

    def test_draft_upload_reuses_existing_signed_files(self):
        key = Path(self.tmp.name) / "key"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
            check=True,
        )
        subprocess.run(
            [
                "openssl",
                "pkey",
                "-in",
                str(key),
                "-pubout",
                "-out",
                str(self.dist / "release.pub"),
            ],
            check=True,
        )
        environment = {
            "GITHUB_REPOSITORY": "owner/firmware",
            "RELEASE_KEY": str(key),
        }
        with (
            patch.object(release, "ROOT", Path(self.tmp.name)),
            patch.dict(os.environ, environment),
            patch.object(sys, "argv", ["release.py", "sign", "v1"]),
        ):
            release.main()

        before = {
            path.name: path.read_bytes()
            for path in self.dist.glob("manifest-*")
        }
        calls = []
        run = subprocess.run

        def command(args, **kwargs):
            if args[0] == "gh":
                calls.append(args)
                return subprocess.CompletedProcess(args, 0)
            return run(args, capture_output=True, **kwargs)

        with (
            patch.object(release, "ROOT", Path(self.tmp.name)),
            patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/firmware"}),
            patch.object(sys, "argv", ["release.py", "draft", "v1"]),
            patch.object(release.subprocess, "run", side_effect=command),
        ):
            os.environ.pop("RELEASE_KEY", None)
            release.main()

        after = {
            path.name: path.read_bytes()
            for path in self.dist.glob("manifest-*")
        }
        self.assertEqual(after, before)
        self.assertEqual(len(calls), 1)
        self.assertIn("--draft", calls[0])

    def test_draft_rejects_changed_payload_before_github(self):
        key = Path(self.tmp.name) / "key"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
            check=True,
        )
        subprocess.run(
            [
                "openssl",
                "pkey",
                "-in",
                str(key),
                "-pubout",
                "-out",
                str(self.dist / "release.pub"),
            ],
            check=True,
        )
        with (
            patch.object(release, "ROOT", Path(self.tmp.name)),
            patch.dict(
                os.environ,
                {
                    "GITHUB_REPOSITORY": "owner/firmware",
                    "RELEASE_KEY": str(key),
                },
            ),
            patch.object(sys, "argv", ["release.py", "sign", "v1"]),
        ):
            release.main()

        (self.dist / "openwrt-q703.tar.gz").write_bytes(b"changed")
        calls = []

        def command(args, **kwargs):
            if args[0] == "gh":
                calls.append(args)
                return subprocess.CompletedProcess(args, 0)
            return subprocess.run(args, capture_output=True, **kwargs)

        with (
            patch.object(release, "ROOT", Path(self.tmp.name)),
            patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/firmware"}),
            patch.object(sys, "argv", ["release.py", "draft", "v1"]),
            patch.object(release.subprocess, "run", side_effect=command),
        ):
            os.environ.pop("RELEASE_KEY", None)
            with self.assertRaises(ValueError):
                release.main()
        self.assertEqual(calls, [])

    def test_invalid_release_mode_reports_usage(self):
        with patch.object(sys, "argv", ["release.py", "publish", "v1"]):
            with self.assertRaisesRegex(SystemExit, "usage:"):
                release.main()


class ReleaseGuideTests(unittest.TestCase):
    def test_host_preflight_failure_does_not_exit_the_interactive_shell(self):
        install = (Path(__file__).parents[1] / "docs/install.md").read_text()

        self.assertIn("recovery_host_preflight()", install)
        self.assertIn("if recovery_host_preflight; then", install)
        self.assertIn('test -z "${MODEL:-}"', install)
        self.assertIn("RECOVERY_PREFLIGHT_OK=1", install)
        self.assertIn("RECOVERY_PREFLIGHT_OK=0", install)
        self.assertIn("H remains open", install)
        self.assertNotIn("set -euo pipefail", install)

    def test_docs_separate_unsigned_recovery_signing_and_publication(self):
        root = Path(__file__).parents[1]
        install = (root / "docs/install.md").read_text()
        update = (root / "docs/update.md").read_text()

        self.assertIn("unsigned RAM recovery", install)
        self.assertIn(
            "does not require a clean Git worktree, a new commit",
            install,
        )
        self.assertIn("EMBEDDED_REPOSITORY", install)
        self.assertIn("pixi run release sign", install)
        self.assertIn("pixi run release verify", install)
        self.assertIn("pixi run build openwrt", install)
        self.assertIn("must not be presented as a reproducible public release", install)
        self.assertIn("pixi run release sign", update)
        self.assertIn("pixi run release draft", update)
        self.assertIn("GitHub draft creation and public publication are optional", update)
        self.assertIn("A public release requires a clean committed source", update)
        self.assertFalse((root / "docs/build.md").exists())
