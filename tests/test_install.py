# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "openwrt/files/usr/sbin/firmware-install"

# Every command capable of changing the host is intercepted. Device I/O is only
# forwarded to dd after validating that its paths are inside the temporary root.
MOCK = r'''#!/usr/bin/env python3
import io, json, os, pathlib, re, shutil, subprocess, sys, tarfile
root = pathlib.Path(os.environ["FIRMWARE_ROOT"])
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
def inside(value):
    path = pathlib.Path(value).resolve()
    assert path.is_relative_to(root.resolve()), value
    return path
def log():
    with (root / "calls").open("a") as stream:
        stream.write(json.dumps([name, *args]) + "\n")
if name == "stat":
    path = inside(args[-1])
    if args[-2] == "%F":
        assert path.parent == root / "dev"
        print("character special file" if path.name.startswith("mtd") else "block special file")
    elif args[-2] == "%T": print("tmpfs" if (root / "ram-backup").exists() else "ext2/ext3")
    elif args[-2] == "%d": print(2 if path == root / "backup" else 1)
    elif args[-2] == "%t:%T":
        group = "mtd" if path.name.startswith("mtd") else "block"
        device = (root / "sys/class" / group / path.name / "dev").read_text().strip()
        print("0:0" if (root / "wrong-device").exists() else ":".join(format(int(v), "x") for v in device.split(":")))
    else: sys.exit(1)
elif name == "blockdev":
    inside(args[-1]); assert args[0] == "--getss"; print(512)
elif name == "flock":
    if (root / "busy-once").exists():
        (root / "busy-once").unlink(); sys.exit(1)
    os.execv("/usr/bin/flock", ["flock", *args])
elif name == "fw_printenv":
    inside(args[1]); print(json.loads((root / "environment").read_text())[args[-1]])
elif name == "fw_setenv":
    inside(args[1]); changes = inside(args[-1]).read_text()
    log()
    state = json.loads((root / "environment").read_text())
    state.update(line.split(" ", 1) for line in changes.splitlines())
    (root / "environment").write_text(json.dumps(state))
elif name == "jsonfilter":
    expr = args[args.index("-e") + 1]
    if "-i" not in args: print("true")
    else:
        data = json.loads(inside(args[args.index("-i") + 1]).read_text())
        match = re.fullmatch(r"@\.files\['([^']+)'\]\.(\w+)", expr)
        print(data["files"][match[1]][match[2]] if match else data[expr[2:]])
elif name == "dd":
    values = dict(arg.split("=", 1) for arg in args)
    for key in ("if", "of"):
        if key in values: inside(values[key])
    log()
    if "skip" in values and (root / "fail-readback").exists():
        sys.stdout.buffer.write(b"wrong kernel")
    else: sys.exit(subprocess.run(["/usr/bin/dd", *args]).returncode)
elif name == "sysupgrade":
    assert args[0] == "-b"
    with tarfile.open(inside(args[1]), "w:gz") as archive:
        data = b"preserved\n"
        member = tarfile.TarInfo("etc/config/test"); member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    log()
elif name == "mount":
    assert args[:2] == ["-t", "ext4"]
    device, target = inside(args[2]), inside(args[3])
    (root / "mount-state").write_text(json.dumps([device.name, str(target)])); log()
elif name == "umount":
    target = inside(args[0]); log()
    device, mounted = json.loads((root / "mount-state").read_text())
    assert str(target) == mounted
    shutil.copytree(target, root / "results" / device, dirs_exist_ok=True)
elif name in ("mkfs.ext4", "e2fsck"):
    assert inside(args[-1]).parent == root / "dev"; log()
elif name == "mtd":
    assert args[0] == "write"
    image, device = inside(args[1]), inside(args[2])
    assert device.parent == root / "dev"
    log()
    with device.open("r+b") as stream:
        stream.write(b"bad" if (root / "fail-nor").exists() else image.read_bytes())
elif name == "pidof":
    assert args == ["qcontrol"]; print(123)
elif name == "ubus": print('{"up":true}')
elif name in ("reboot", "sync", "logger", "sleep"): log()
else: raise AssertionError(name)
'''


class InstallTests(unittest.TestCase):
    BOARD = "q703"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="firmware-install-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ("usr/lib", "usr/sbin", "bin", "dev", "backup", "stage", "results"):
            (self.root / directory).mkdir(parents=True)
        shutil.copyfile(ROOT / "openwrt/files/usr/lib/firmware.sh", self.root / "usr/lib/firmware.sh")
        self.installer = self.root / "usr/sbin/firmware-install"
        shutil.copyfile(INSTALLER, self.installer)
        self.installer.chmod(0o755)
        self.write("etc/firmware/repository", "owner/firmware\n")
        self.write("etc/firmware/installed", "A 10\n")
        self.write("etc/firmware/boot.env", "firmware_layout=1\nbootcmd=run boot_ab\n")
        self.compatible = {"q703": "fujitsu,q703", "ts221": "qnap,ts221"}[self.BOARD]
        self.write("proc/device-tree/compatible", self.compatible + "\0marvell,kirkwood\0")
        self.write("proc/self/mountinfo", "20 1 9:0 / / rw - ext4 /dev/md0 rw\n")
        self.write("proc/mounts", "/dev/md0 / ext4 rw 0 0\n")
        self.write("proc/cmdline", "firmware.layout=1 firmware.slot=A\n")
        self.write("sys/class/net/eth0/carrier", "1\n")
        self.state = {"firmware_layout": "1", "active_slot": "A", "rollback_slot": "A",
                      "release_sequence": "10", "upgrade_available": "0", "bootcount": "0"}
        self.save_state()
        for number in (0, 1):
            base = f"sys/class/block/md{number}"
            self.write(f"{base}/dev", f"9:{number}\n")
            for key, value in {"level": "raid1", "metadata_version": "0.90", "raid_disks": "2",
                               "degraded": "0", "sync_action": "idle"}.items():
                self.write(f"{base}/md/{key}", value + "\n")
            for disk in ("sda", "sdb"):
                self.write(f"{base}/slaves/{disk}{number + 1}", "")
            self.write(f"dev/md{number}", "")
        for disk, major in (("sda", "8"), ("sdb", "65")):
            self.write(f"sys/class/block/{disk}/dev", f"{major}:0\n")
            with (self.root / "dev" / disk).open("wb") as stream:
                stream.truncate(150 * 1024 * 1024)
            for part in (1, 2):
                base = f"sys/class/block/{disk}{part}"
                self.write(f"{base}/dev", f"{major}:{part}\n")
                self.write(f"{base}/start", str(264192 + (part - 1) * 2097152))
                self.write(f"{base}/size", "2097152")
        for number, name, offset, size in ((0, "U-Boot", 0, 524288),
                                           (3, "RootFS2", 13631488, 3145728)):
            for key, value in {"name": name, "offset": offset, "size": size,
                               "erasesize": 65536, "type": "nor", "dev": f"90:{number * 2}"}.items():
                self.write(f"sys/class/mtd/mtd{number}/{key}", str(value) + "\n")
            (self.root / f"dev/mtd{number}").write_bytes(b"\xff" * size)
        for name in ("stat", "blockdev", "fw_printenv", "fw_setenv", "jsonfilter", "dd",
                     "sysupgrade", "mount", "umount", "mkfs.ext4", "e2fsck", "mtd",
                     "pidof", "ubus", "reboot", "sync", "logger", "sleep", "flock"):
            path = self.root / "bin" / name
            path.write_text(MOCK)
            path.chmod(0o755)
        self.env = {**os.environ, "FIRMWARE_ROOT": str(self.root),
                    "PATH": str(self.root / "bin") + ":" + os.environ["PATH"]}
        self.command("openssl", "genpkey", "-algorithm", "ED25519", "-out", str(self.root / "key"))
        self.command("openssl", "pkey", "-in", str(self.root / "key"), "-pubout",
                     "-out", str(self.root / "etc/firmware/release.pub"))
        inner = self.archive({"./etc/firmware/release.pub": (self.root / "etc/firmware/release.pub").read_bytes(),
                              "./etc/firmware/repository": b"owner/firmware\n",
                              "./etc/uci-defaults/ts221": b"new image defaults\n"})
        self.kernel = bytes.fromhex("27051956") + b"K" * 1020
        bundle = self.archive({"./rootfs.tar.gz": inner, "./kernel.uImage": self.kernel})
        self.info = {"schema": 2, "layout": 1, "board": self.compatible,
                     "repository": "owner/firmware", "tag": "v20", "sequence": 20, "files": {}}
        for name, data in ((f"openwrt-{self.BOARD}.tar.gz", bundle),
                           (f"u-boot-{self.BOARD}.kwb", b"bootloader" * 64)):
            (self.root / "stage" / name).write_bytes(data)
            self.info["files"][name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        self.sign()

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)

    def command(self, *args):
        subprocess.run(args, check=True, capture_output=True)

    def archive(self, files):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return output.getvalue()

    def save_state(self):
        self.write("environment", json.dumps(self.state))

    def sign(self):
        self.write("stage/manifest.json", json.dumps(self.info))
        self.command("openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(self.root / "key"),
                     "-in", str(self.root / "stage/manifest.json"),
                     "-out", str(self.root / "stage/manifest.sig"))

    def install(self, mode="upgrade", ack=None):
        args = [mode]
        if mode not in ("status", "confirm", "check"):
            args.append(str(self.root / "stage"))
        if mode in ("init", "u-boot"):
            args.extend([str(self.root / "backup"), ack or "missing-ack"])
        return subprocess.run(["sh", str(self.installer), *args], env=self.env,
                              capture_output=True, text=True, timeout=20)

    def calls(self):
        path = self.root / "calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_no_writes(self):
        self.assertFalse({call[0] for call in self.calls()} & {"mkfs.ext4", "fw_setenv", "mtd", "reboot"})

    def test_upgrade_writes_only_inactive_slot_and_switches_last(self):
        status = self.install("status")
        self.assertEqual((status.returncode, status.stdout), (0, "10\n"), status.stderr)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([c[-1] for c in calls if c[0] == "mkfs.ext4"], [str(self.root / "dev/md1")])
        for disk in ("sda", "sdb"):
            with (self.root / "dev" / disk).open("rb") as stream:
                stream.seek(2048 * 512)
                self.assertEqual(stream.read(len(self.kernel)), bytes(len(self.kernel)))
                stream.seek(133120 * 512)
                self.assertEqual(stream.read(len(self.kernel)), self.kernel)
        self.assertEqual((self.root / "results/md1/etc/firmware/installed").read_text(), "B 20\n")
        self.assertEqual((self.root / "results/md1/etc/config/test").read_text(), "preserved\n")
        self.assertFalse((self.root / "results/md1/etc/uci-defaults/ts221").exists())
        state = json.loads((self.root / "environment").read_text())
        self.assertEqual([state[k] for k in ("active_slot", "rollback_slot", "release_sequence", "upgrade_available")],
                         ["B", "A", "20", "1"])
        self.assertEqual([c[0] for c in calls[-2:]], ["fw_setenv", "logger"])
        self.assertNotIn("reboot", [c[0] for c in calls])
        self.assertNotIn("mtd", [c[0] for c in calls])

    def test_rejects_mounted_target_or_member(self):
        for device in ("9:1", "8:2"):
            with self.subTest(device=device):
                self.write("proc/self/mountinfo", f"20 1 9:0 / / rw - ext4 /dev/md0 rw\n21 1 {device} / /mnt rw - ext4 target rw\n")
                result = self.install()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("is mounted", result.stderr)
                self.assert_no_writes()

    def test_rejects_degraded_or_overlapping_layout(self):
        for path, value, message in (("sys/class/block/md1/md/degraded", "1", "degraded"),
                                     ("sys/class/block/sdb2/start", "100000", "overlaps")):
            with self.subTest(path=path):
                old = (self.root / path).read_text()
                self.write(path, value)
                result = self.install()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assert_no_writes()
                self.write(path, old)

    def test_rejects_bad_signature_and_wrong_model(self):
        self.write("stage/manifest.json", "{}")
        self.assertNotEqual(self.install().returncode, 0)
        self.info["board"] = "qnap,ts221" if self.BOARD == "q703" else "fujitsu,q703"
        self.sign()
        self.assertNotEqual(self.install().returncode, 0)
        self.assert_no_writes()

    def test_rejects_device_node_sysfs_mismatch(self):
        self.write("wrong-device", "")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("device node/sysfs mismatch", result.stderr)
        self.assert_no_writes()

    def test_rejects_replay_and_pending_trial(self):
        self.state["release_sequence"] = "20"
        self.save_state()
        self.assertIn("not newer", self.install().stderr)
        self.state["release_sequence"] = "10"
        self.state["upgrade_available"] = "1"
        self.save_state()
        self.assertNotEqual(self.install("status").returncode, 0)
        self.assertIn("current trial", self.install().stderr)
        self.assert_no_writes()

    def test_readback_failure_keeps_environment_and_does_not_reboot(self):
        self.write("fail-readback", "")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("readback failed", result.stderr)
        self.assertEqual(json.loads((self.root / "environment").read_text()), self.state)
        self.assertFalse({c[0] for c in self.calls()} & {"fw_setenv", "reboot"})

    def test_confirm_trial_and_rollback_preserve_highwater(self):
        for rollback, installed in ((False, 20), (True, 10), (True, 20)):
            with self.subTest(rollback=rollback, installed=installed):
                self.state.update(active_slot="B" if rollback else "A", rollback_slot="A",
                                  release_sequence="20", upgrade_available="1", bootcount="2")
                self.save_state()
                self.write("etc/firmware/installed", f"A {installed}\n")
                result = self.install("confirm")
                self.assertEqual(result.returncode, 0, result.stderr)
                state = json.loads((self.root / "environment").read_text())
                self.assertEqual([state[k] for k in ("active_slot", "rollback_slot", "release_sequence", "upgrade_available", "bootcount")],
                                 ["A", "A", "20", "0", "0"])

    def test_confirm_rejects_missing_health_and_wrong_identity(self):
        self.state.update(release_sequence="20", upgrade_available="1")
        self.save_state()
        self.assertNotEqual(self.install("confirm").returncode, 0)
        self.write("etc/firmware/installed", "A 20\n")
        self.write("sys/class/net/eth0/carrier", "0\n")
        self.assertNotEqual(self.install("confirm").returncode, 0)
        self.assert_no_writes()

    def test_boot_check_skips_ram_and_uninitialized_system(self):
        self.write("proc/mounts", "rootfs / rootfs rw 0 0\n")
        self.assertEqual(self.install("check").returncode, 0)
        self.write("proc/mounts", "/dev/md0 / ext4 rw 0 0\n")
        (self.root / "etc/firmware/installed").unlink()
        self.assertEqual(self.install("check").returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_boot_check_reboots_failed_trial_and_confirms_healthy_trial(self):
        self.state.update(release_sequence="20", upgrade_available="1")
        self.save_state()
        self.write("etc/firmware/installed", "A 20\n")
        self.write("sys/class/net/eth0/carrier", "0\n")
        self.assertNotEqual(self.install("check").returncode, 0)
        self.assertEqual([c[0] for c in self.calls()], ["sleep", "logger", "reboot"])
        self.assertEqual(json.loads((self.root / "environment").read_text()), self.state)
        (self.root / "calls").unlink()
        self.write("sys/class/net/eth0/carrier", "1\n")
        result = self.install("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c[0] for c in self.calls()], ["sleep", "fw_setenv", "logger"])

    def test_init_requires_ack_ram_and_unmounted_arrays(self):
        self.assertNotEqual(self.install("init").returncode, 0)
        self.assertIn("RAM recovery", self.install("init", "--erase-system").stderr)
        self.write("proc/mounts", "rootfs / rootfs rw 0 0\n")
        self.assertIn("is mounted", self.install("init", "--erase-system").stderr)
        self.assert_no_writes()

    def test_boot_check_defers_for_installation_lock_without_rebooting(self):
        self.write("busy-once", "")
        result = self.install("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c[1] for c in self.calls() if c[0] == "sleep"], ["60", "10"])
        self.assert_no_writes()

    def test_rollback_stays_running_when_network_or_inactive_array_is_down(self):
        self.state.update(active_slot="B", rollback_slot="A", release_sequence="20", upgrade_available="1")
        self.save_state()
        self.write("sys/class/net/eth0/carrier", "0\n")
        self.write("sys/class/block/md1/md/degraded", "1\n")
        result = self.install("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((self.root / "environment").read_text())
        self.assertEqual((state["active_slot"], state["upgrade_available"], state["release_sequence"]),
                         ("A", "0", "20"))
        self.assertNotIn("reboot", [call[0] for call in self.calls()])

    def test_init_backs_up_nor_and_populates_both_slots(self):
        self.write("proc/mounts", "rootfs / rootfs rw 0 0\n")
        self.write("proc/self/mountinfo", "20 1 0:1 / / rw - rootfs rootfs rw\n")
        result = self.install("init", "--erase-system")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([Path(c[-1]).name for c in self.calls() if c[0] == "mkfs.ext4"], ["md0", "md1"])
        for number, slot in ((0, "A"), (1, "B")):
            self.assertEqual((self.root / f"results/md{number}/etc/firmware/installed").read_text(), f"{slot} 20\n")
        backup = next((self.root / "backup").iterdir())
        self.assertEqual((backup / "RootFS2.bin").stat().st_size, 3145728)
        self.assertEqual((backup / "u-boot.bin").stat().st_size, 524288)
        self.assertNotIn("reboot", [c[0] for c in self.calls()])

    def test_nor_requires_ack_geometry_and_external_backup(self):
        self.assertNotEqual(self.install("u-boot").returncode, 0)
        self.write("sys/class/mtd/mtd0/size", "524287\n")
        self.assertIn("geometry", self.install("u-boot", "--flash-nor").stderr)
        self.write("sys/class/mtd/mtd0/size", "524288\n")
        self.write("ram-backup", "")
        self.assertIn("RAM", self.install("u-boot", "--flash-nor").stderr)
        self.assert_no_writes()

    def test_nor_backup_precedes_write_and_readback(self):
        result = self.install("u-boot", "--flash-nor")
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = next((self.root / "backup").iterdir())
        self.assertEqual((backup / "u-boot.bin").read_bytes(), b"\xff" * 524288)
        image = (self.root / f"stage/u-boot-{self.BOARD}.kwb").read_bytes()
        self.assertEqual((self.root / "dev/mtd0").read_bytes()[:len(image)], image)
        self.assertEqual([c[0] for c in self.calls()], ["dd", "dd", "sync", "mtd"])
        self.assertEqual(json.loads((self.root / "environment").read_text()), self.state)

    def test_nor_failed_readback_never_reboots_or_restores(self):
        self.write("fail-nor", "")
        result = self.install("u-boot", "--flash-nor")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NOR readback failed", result.stderr)
        self.assertEqual([c[0] for c in self.calls()].count("mtd"), 1)
        self.assertNotIn("reboot", [c[0] for c in self.calls()])


class Ts221InstallTests(InstallTests):
    BOARD = "ts221"
