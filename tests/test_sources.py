# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
from contextlib import redirect_stderr
import importlib.util
import io
import json
import socket
import runpy
import sys
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

spec = importlib.util.spec_from_file_location("sources", Path(__file__).parents[1] / "scripts/sources.py")
sources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sources)
ROOT = Path(__file__).parents[1]


class Sources(unittest.TestCase):
    def test_cli_network_failure_is_a_short_nonzero_error(self):
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", ["sources.py", "resolve"]),
            patch("urllib.request.urlopen", side_effect=URLError(
                socket.gaierror(-2, "Name or service not known"))),
            redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(ROOT / "scripts/sources.py"), run_name="__main__")
        self.assertEqual(result.exception.code, 1)
        self.assertIn("STOP: Cannot fetch https://ftp.denx.de/pub/u-boot/", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_dns_failure_names_url_and_preserves_source_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "sources.json"
            lock.write_text('{"previous": "lock"}\n')
            before = lock.read_bytes()
            with (
                patch.object(sources, "LOCK", lock),
                patch.object(sources, "urlopen", side_effect=URLError(
                    socket.gaierror(-2, "Name or service not known"))),
            ):
                with self.assertRaisesRegex(sources.SourceNetworkError,
                                            r"https://ftp.denx.de/pub/u-boot/.*Name or service not known"):
                    sources.resolve()
            self.assertEqual(lock.read_bytes(), before)

    def test_refresh_reextracts_only_the_selected_verified_source(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            work = Path(temporary)
            archive = work / "source.tar"
            with tarfile.open(archive, "w") as output:
                member = tarfile.TarInfo("source/pristine")
                member.size = 5
                output.addfile(member, io.BytesIO(b"clean"))
            source_definition = {
                "url": "https://example.com/source",
                "sha256": sources.digest(archive),
            }
            source = work / "u-boot"
            source.mkdir()
            (source / ".source").write_text(source_definition["sha256"] + "\n")
            (source / "generated").write_text("discard me")
            preserved = work / "archives/locked"
            preserved.parent.mkdir()
            preserved.write_text("keep me")

            with (
                patch.object(sources, "WORK", work),
                patch.object(sources, "download", return_value=archive),
            ):
                sources.refresh(source_definition, source)

            self.assertEqual((source / "pristine").read_text(), "clean")
            self.assertFalse((source / "generated").exists())
            self.assertFalse((work / ".u-boot.refreshing").exists())
            self.assertEqual(preserved.read_text(), "keep me")

    def test_refresh_refuses_an_unmarked_or_symlinked_source(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            work = Path(temporary)
            source = work / "openwrt"
            source.mkdir()
            definition = {"url": "https://example.com/source", "sha256": "a" * 64}
            with patch.object(sources, "WORK", work), self.assertRaises(ValueError):
                sources.refresh(definition, source)
            source.rmdir()
            source.symlink_to(work / "elsewhere", target_is_directory=True)
            with patch.object(sources, "WORK", work), self.assertRaises(ValueError):
                sources.refresh(definition, source)

    def test_fetch_refreshes_a_previous_version_only_with_its_verified_archive(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            work = Path(temporary)
            archives = work / "archives"
            archives.mkdir()
            old_archive = archives / "old.tar"
            new_archive = archives / "new.tar"
            for archive, content in ((old_archive, b"old"), (new_archive, b"new")):
                with tarfile.open(archive, "w") as output:
                    member = tarfile.TarInfo("source/value")
                    member.size = len(content)
                    output.addfile(member, io.BytesIO(content))
            old_digest = sources.digest(old_archive)
            new_digest = sources.digest(new_archive)
            old_archive.rename(archives / old_digest)
            new_archive.rename(archives / new_digest)
            destination = work / "linux"
            destination.mkdir()
            (destination / ".source").write_text(old_digest + "\n")
            (destination / "generated").write_text("discard")
            definition = {
                "url": "https://example.com/new",
                "sha256": new_digest,
            }

            with (
                patch.object(sources, "WORK", work),
                patch.object(sources, "download", return_value=archives / new_digest),
            ):
                sources.extract(definition, destination)

            self.assertEqual((destination / "value").read_bytes(), b"new")
            self.assertEqual((destination / ".source").read_text(), new_digest + "\n")
            self.assertFalse((destination / "generated").exists())

            (destination / ".source").write_text(old_digest + "\n")
            (archives / old_digest).write_bytes(b"tampered")
            with (
                patch.object(sources, "WORK", work),
                self.assertRaises(ValueError),
            ):
                sources.extract(definition, destination)

    def test_numeric_final_versions(self):
        self.assertEqual(sources.latest(["2026.07-rc5", "2026.07", "2026.07.1", "2026.10-rc1"]), "2026.07.1")
        self.assertEqual(sources.latest(["25.12.9", "25.12.10", "26.01.0-rc1"]), "25.12.10")
        with self.assertRaises(ValueError):
            sources.latest(["7.3-rc1"])

    def test_official_feeds(self):
        commit = "a" * 40
        with patch.object(sources, "pin", return_value={"commit": commit}) as pin:
            result = sources.feeds(f"# comment\nsrc-git packages https://git.openwrt.org/feed/packages.git^{commit}\nsrc-git video https://github.com/openwrt/video.git;openwrt-25.12\n")
        self.assertEqual(set(result), {"packages", "video"})
        self.assertEqual(pin.call_args_list[0].args, ("openwrt/packages", commit))
        self.assertEqual(pin.call_args_list[1].args, ("openwrt/video", "openwrt-25.12"))

    def test_unknown_or_unpinned_feed_rejected(self):
        for line in ("", "src-link packages /tmp/packages", "src-git packages https://evil.example/packages.git;main", "src-git packages https://github.com/openwrt/packages.git^main"):
            with self.subTest(line=line), self.assertRaises(ValueError):
                sources.feeds(line)

    def test_https_required(self):
        with self.assertRaises(ValueError):
            sources.request("http://example.com/source.tar.gz")

    def test_invalid_project_commit_rejected_before_network(self):
        with patch.dict("os.environ", {"GITHUB_SHA": "main"}), self.assertRaises(ValueError):
            sources.resolve()

    def test_resolve_freezes_all_inputs(self):
        commit = "a" * 40
        pages = {
            "https://ftp.denx.de/pub/u-boot/": '<a href="u-boot-2026.07.1.tar.bz2"><a href="u-boot-2026.10-rc1.tar.bz2">',
            "https://downloads.openwrt.org/releases/": '<a href="25.12.5/"><a href="26.01.0-rc1/">',
            "https://www.kernel.org/releases.json": json.dumps({
                "latest_stable": {"version": "7.2.3"},
                "releases": [{"version": "7.2.3", "iseol": False,
                              "released": {"timestamp": 1},
                              "source": "https://cdn.kernel.org/linux-7.2.3.tar.xz"}],
            }),
            f"https://raw.githubusercontent.com/openwrt/openwrt/{commit}/feeds.conf.default": f"src-git packages https://git.openwrt.org/feed/packages.git^{commit}",
        }
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            lockfile = Path(temporary) / "sources.json"
            with (
                patch.object(sources, "WORK", Path(temporary)),
                patch.object(sources, "LOCK", lockfile),
                patch.object(sources, "read", side_effect=pages.__getitem__),
                patch.object(sources, "pin", side_effect=lambda *args: {"commit": commit}) as pin,
                patch.object(sources, "download", return_value=Path(temporary) / "linux") as download,
                patch.dict("os.environ", {"GITHUB_SHA": commit}),
            ):
                lock = sources.resolve()
            self.assertEqual(json.loads(lockfile.read_text()), lock)
            self.assertEqual(lock["project_commit"], commit)
            self.assertEqual(lock["openwrt"]["feeds"]["packages"]["commit"], commit)
            self.assertEqual([call.args[1] for call in pin.call_args_list], ["v2026.07.1", "v25.12.5", commit])
            download.assert_called_once_with("https://cdn.kernel.org/linux-7.2.3.tar.xz")

    def test_kernel_rejects_invalid_official_metadata(self):
        for release in (
            {"version": "7.2-rc1", "released": {"timestamp": 1}, "source": "https://example.com/linux"},
            {"version": "7.2.3", "released": {"timestamp": 0}, "source": "https://example.com/linux"},
        ):
            with self.subTest(release=release), self.assertRaises(ValueError):
                sources.kernel(release)

    def test_archive_hash_rechecked(self):
        with self.assertRaises(ValueError):
            sources.download("https://example.com/source", "../outside")
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary, patch.object(sources, "WORK", Path(temporary)):
            expected = hashlib.sha256(b"source").hexdigest()
            with patch.object(sources, "request", return_value=io.BytesIO(b"source")):
                path = sources.download("https://example.com/source", expected)
            path.write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                sources.download("https://example.com/source", expected)

    def test_safe_extraction_and_reuse(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary, patch.object(sources, "WORK", Path(temporary)):
            archive = Path(temporary) / "source.tar"
            with tarfile.open(archive, "w") as tar:
                member = tarfile.TarInfo("source/file")
                member.size = 4
                tar.addfile(member, io.BytesIO(b"data"))
                link = tarfile.TarInfo("source/link")
                link.type, link.linkname = tarfile.SYMTYPE, "file"
                tar.addfile(link)
            source = {"url": "https://example.com/source", "sha256": sources.digest(archive)}
            destination = Path(temporary) / "tree"
            with patch.object(sources, "download", return_value=archive) as download:
                sources.extract(source, destination)
                self.assertEqual((destination / "link").read_bytes(), b"data")
                sources.extract(source, destination)
                download.assert_called_once()
                with self.assertRaises(ValueError):
                    sources.extract({**source, "sha256": "b" * 64}, destination)

    def test_archive_escape_rejected(self):
        for name, target in (("../escape", None), ("source/link", "../../escape")):
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=ROOT) as temporary, patch.object(sources, "WORK", Path(temporary)):
                archive = Path(temporary) / "source.tar"
                with tarfile.open(archive, "w") as tar:
                    member = tarfile.TarInfo(name)
                    if target:
                        member.type, member.linkname = tarfile.SYMTYPE, target
                    tar.addfile(member)
                source = {"url": "https://example.com/source", "sha256": sources.digest(archive)}
                with patch.object(sources, "download", return_value=archive), self.assertRaises(tarfile.FilterError):
                    sources.extract(source, Path(temporary) / "tree")


if __name__ == "__main__":
    unittest.main()
