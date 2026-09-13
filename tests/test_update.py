# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "openwrt/files/usr/sbin/firmware-update"


class UpdateTests(unittest.TestCase):
    BOARD = "q703"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("etc/firmware", "proc/device-tree", "remote", "bin", "usr/lib", "usr/sbin"):
            (self.root / name).mkdir(parents=True)
        shutil.copyfile(ROOT / "openwrt/files/usr/lib/firmware.sh", self.root / "usr/lib/firmware.sh")
        (self.root / "proc/mounts").write_text("/dev/md0 / ext4 rw 0 0\n")
        compatible = {"q703": "fujitsu,q703", "ts221": "qnap,ts221"}[self.BOARD]
        (self.root / "proc/device-tree/compatible").write_bytes(f"{compatible}\0marvell,kirkwood\0".encode())
        (self.root / "etc/firmware/repository").write_text("owner/firmware\n")
        self.run_command("openssl", "genpkey", "-algorithm", "ED25519", "-out", str(self.root / "key"))
        self.run_command("openssl", "pkey", "-in", str(self.root / "key"), "-pubout",
                         "-out", str(self.root / "etc/firmware/release.pub"))
        self.env = {**os.environ, "FIRMWARE_ROOT": str(self.root),
                    "PATH": str(self.root / "bin") + ":" + os.environ["PATH"]}
        self.helper("curl", '''import os, pathlib, shutil, sys
root = pathlib.Path(os.environ["FIRMWARE_ROOT"])
url = next(a for a in sys.argv if a.startswith("https://"))
name = url.rsplit("/", 1)[1]
with (root / "requests").open("a") as f: f.write(url + "\\n")
shutil.copyfile(root / "remote" / name, sys.argv[sys.argv.index("-o") + 1])
''')
        self.helper("jsonfilter", '''import json, re, sys
data = json.load(open(sys.argv[sys.argv.index("-i") + 1]))
expr = sys.argv[sys.argv.index("-e") + 1]
match = re.fullmatch(r"@\\.files\\['([^']+)'\\]\\.(\\w+)", expr)
value = data["files"][match[1]][match[2]] if match else data[expr[2:]]
print(value)
''')
        self.helper("logger", 'import sys\nprint("logger:", *sys.argv[1:], file=sys.stderr)')
        self.helper("firmware-install", '''import fcntl, json, os, pathlib, sys
root = pathlib.Path(os.environ["FIRMWARE_ROOT"])
if sys.argv[1] == "status":
    if (root / "blocked").exists(): sys.exit(1)
    print((root / "highwater").read_text() if (root / "highwater").exists() else "0")
else:
    assert sys.argv[1] == "upgrade", "automatic NOR flash is forbidden"
    with (root / "root/updates/.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with (root / "installs").open("a") as log: log.write(sys.argv[2] + "\\n")
    if (root / "fail-install").exists(): sys.exit(1)
    info = json.loads((pathlib.Path(sys.argv[2]) / "manifest.json").read_text())
    (root / "highwater").write_text(str(info["sequence"]))
''')
        (self.root / "bin/firmware-install").rename(self.root / "usr/sbin/firmware-install")
        self.info = {"schema": 2, "layout": 1, "board": compatible, "repository": "owner/firmware",
                     "tag": "v1", "sequence": 100, "files": {}}
        self.files = (f"openwrt-{self.BOARD}.tar.gz", f"u-boot-{self.BOARD}.kwb")
        for name in self.files:
            data = name.encode()
            (self.root / "remote" / name).write_bytes(data)
            self.info["files"][name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        self.sign()

    def run_command(self, *args):
        subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def helper(self, name, code):
        output = self.root / "bin" / name
        output.write_text("#!/usr/bin/env python3\n" + code)
        output.chmod(0o755)

    def sign(self):
        manifest = self.root / f"remote/manifest-{self.BOARD}.json"
        manifest.write_text(json.dumps(self.info))
        self.run_command("openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(self.root / "key"),
                         "-in", str(manifest), "-out", str(manifest.with_suffix(".sig")))

    def update(self):
        return subprocess.run(["sh", str(UPDATER)], env=self.env, capture_output=True, text=True)

    def test_stages_signed_files_and_installs_os_only(self):
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "highwater").read_text(), "100")
        self.assertEqual((self.root / "installs").read_text(), str(self.root / "root/updates/v1") + "\n")
        self.assertFalse((self.root / "root/updates/sequence").exists())
        requests = (self.root / "requests").read_text().splitlines()
        base = "https://github.com/owner/firmware/releases"
        self.assertEqual(requests, [f"{base}/latest/download/manifest-{self.BOARD}.json",
                                   f"{base}/latest/download/manifest-{self.BOARD}.sig",
                                   *[f"{base}/download/v1/{name}" for name in self.files]])
        self.assertTrue((self.root / "root/updates/v1" / self.files[1]).exists())

    def enable_nor_auto(self, nor_bytes: bytes, backup_mounted=True):
        (self.root / "highwater").write_text("100")   # release already confirmed
        (self.root / "nor_auto").write_text("1")
        sysfs = self.root / "sys/class/mtd/mtd0"
        sysfs.mkdir(parents=True)
        (sysfs / "name").write_text("U-Boot")
        (sysfs / "offset").write_text("0")
        (sysfs / "size").write_text("524288")
        (self.root / "dev").mkdir(exist_ok=True)
        (self.root / "dev/mtd0").write_bytes(nor_bytes)
        mounts = "/dev/md0 / ext4 rw 0 0\n"
        if backup_mounted:
            (self.root / "backup").mkdir(exist_ok=True)
            mounts += "/dev/sda3 /backup ext4 rw 0 0\n"
        (self.root / "proc/mounts").write_text(mounts)
        self.helper("uci", '''import os, pathlib, sys
root = pathlib.Path(os.environ["FIRMWARE_ROOT"])
key = sys.argv[3] if len(sys.argv) > 3 else ""
if key == "firmware.nor.auto":
    print((root / "nor_auto").read_text().strip())
elif key == "firmware.nor.backup_dir":
    print("/backup")
''')
        mock = '''import os, sys
if sys.argv[1] == "status":
    root = os.environ["FIRMWARE_ROOT"]
    with open(root + "/highwater") as f: print(f.read().strip())
elif sys.argv[1] == "u-boot":
    with open(os.environ["FIRMWARE_ROOT"] + "/nor-flashes", "a") as log:
        log.write(sys.argv[2] + " " + sys.argv[3] + "\\n")
else:
    raise SystemExit("unexpected firmware-install mode: " + sys.argv[1])
'''
        installer = self.root / "usr/sbin/firmware-install"
        installer.write_text("#!/usr/bin/env python3\n" + mock)
        installer.chmod(0o755)
        self.helper("firmware-install", '''import fcntl, json, os, pathlib, sys
root = pathlib.Path(os.environ["FIRMWARE_ROOT"])
if sys.argv[1] == "status":
    if (root / "blocked").exists(): sys.exit(1)
    print((root / "highwater").read_text() if (root / "highwater").exists() else "0")
elif sys.argv[1] == "u-boot":
    with (root / "nor-flashes").open("a") as log:
        log.write(sys.argv[2] + " " + sys.argv[3] + "\\n")
else:
    assert sys.argv[1] == "upgrade", "unexpected firmware-install mode"
    with (root / "root/updates/.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with (root / "installs").open("a") as log: log.write(sys.argv[2] + "\\n")
    if (root / "fail-install").exists(): sys.exit(1)
    info = json.loads((pathlib.Path(sys.argv[2]) / "manifest.json").read_text())
    (root / "highwater").write_text(str(info["sequence"]))
''')

    def test_nor_auto_disabled_leaves_nor_alone(self):
        (self.root / "highwater").write_text("100")   # release already confirmed
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "nor-flashes").exists())
        self.assertNotIn("u-boot", (self.root / "requests").read_text())

    def test_nor_auto_flashes_when_release_differs(self):
        self.enable_nor_auto(b"old stock bootloader")
        self.update()
        flashes = (self.root / "nor-flashes")
        self.assertTrue(flashes.exists())
        line = flashes.read_text().splitlines()[0]
        self.assertTrue(line.startswith(str(self.root / "root/updates") + "/.fetch."))
        self.assertTrue(line.endswith(" /backup"))

    def test_nor_auto_skips_when_nor_matches_release(self):
        self.enable_nor_auto(self.files[1].encode())
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "nor-flashes").exists())

    def test_nor_auto_requires_backup_mount(self):
        self.enable_nor_auto(b"old stock bootloader", backup_mounted=False)
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "nor-flashes").exists())

    def test_bad_signature_does_not_download_payload(self):
        (self.root / f"remote/manifest-{self.BOARD}.json").write_text("{}")
        self.assertNotEqual(self.update().returncode, 0)
        self.assertNotIn("openwrt-", (self.root / "requests").read_text())
        self.assertFalse((self.root / "installs").exists())

    def test_bad_hash_does_not_advance(self):
        (self.root / "remote" / self.files[0]).write_bytes(b"x" * len(self.files[0]))
        self.assertNotEqual(self.update().returncode, 0)
        self.assertFalse((self.root / "highwater").exists())
        self.assertFalse((self.root / "installs").exists())

    def test_replay_does_not_refetch_payloads(self):
        self.assertEqual(self.update().returncode, 0)
        (self.root / "requests").unlink()
        self.info["sequence"] = 99
        self.sign()
        self.assertEqual(self.update().returncode, 0)
        self.assertNotIn("openwrt-", (self.root / "requests").read_text())

    def test_ram_root_skips_network(self):
        (self.root / "proc/mounts").write_text("rootfs / rootfs rw 0 0\n")
        self.assertEqual(self.update().returncode, 0)
        self.assertFalse((self.root / "requests").exists())

    def test_unknown_board_skips_network(self):
        (self.root / "proc/device-tree/compatible").write_bytes(b"other,board\0marvell,kirkwood\0")
        self.assertNotEqual(self.update().returncode, 0)
        self.assertFalse((self.root / "requests").exists())

    def test_disk_space_reserve(self):
        self.helper("df", 'print("Filesystem 1024-blocks Used Available Capacity Mounted on\\n/dev/md0 1000 990 10 99% /")\n')
        self.assertNotEqual(self.update().returncode, 0)
        self.assertNotIn("openwrt-", (self.root / "requests").read_text())

    def test_wrong_board_or_repository_or_tag(self):
        other = "qnap,ts221" if self.BOARD == "q703" else "fujitsu,q703"
        for key, value in (("board", other), ("repository", "other/firmware"), ("tag", "v1/../x"),
                           ("schema", 1), ("layout", 2), ("sequence", -1)):
            with self.subTest(key=key):
                old = self.info[key]
                self.info[key] = value
                self.sign()
                self.assertNotEqual(self.update().returncode, 0)
                self.assertNotIn("openwrt-", (self.root / "requests").read_text())
                self.info[key] = old

    def test_other_board_payloads_are_rejected(self):
        other = "ts221" if self.BOARD == "q703" else "q703"
        self.info["files"] = {name.replace(self.BOARD, other): info
                              for name, info in self.info["files"].items()}
        self.sign()
        self.assertNotEqual(self.update().returncode, 0)
        self.assertNotIn("openwrt-", (self.root / "requests").read_text())

    def test_retries_failed_install_from_staged_directory(self):
        (self.root / "fail-install").touch()
        self.assertNotEqual(self.update().returncode, 0)
        self.assertFalse((self.root / "highwater").exists())
        self.assertTrue((self.root / "root/updates/v1/manifest.json").exists())
        (self.root / "fail-install").unlink()
        self.assertEqual(self.update().returncode, 0)
        self.assertEqual(len((self.root / "installs").read_text().splitlines()), 2)
        self.assertEqual((self.root / "highwater").read_text(), "100")

    def test_pending_or_uninitialized_root_skips_network(self):
        (self.root / "blocked").touch()
        self.assertEqual(self.update().returncode, 0)
        self.assertFalse((self.root / "requests").exists())
        self.assertFalse((self.root / "installs").exists())

    def test_cache_sequence_is_not_authoritative(self):
        cache = self.root / "root/updates"
        cache.mkdir(parents=True)
        (cache / "sequence").write_text("9999999999\n")
        self.assertEqual(self.update().returncode, 0)
        self.assertTrue((self.root / "installs").exists())

    def test_invalid_environment_sequence_skips_network(self):
        (self.root / "highwater").write_text("not-a-number")
        self.assertNotEqual(self.update().returncode, 0)
        self.assertFalse((self.root / "requests").exists())


class Ts221UpdateTests(UpdateTests):
    BOARD = "ts221"
