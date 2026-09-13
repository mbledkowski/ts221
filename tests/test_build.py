# SPDX-License-Identifier: AGPL-3.0-or-later
import gzip
import io
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SetupTests(unittest.TestCase):
    def test_pixi_declares_a_locked_build_and_check_environment(self):
        config = tomllib.loads((ROOT / "pixi.toml").read_text())
        pixi_config = tomllib.loads((ROOT / ".pixi/config.toml").read_text())
        self.assertIs(pixi_config["detached-environments"], True)
        self.assertEqual(config["workspace"]["platforms"], ["linux-64"])
        self.assertEqual(config["workspace"]["requires-pixi"], ">=0.79,<0.80")
        self.assertEqual(config["environments"]["default"]["solve-group"], "firmware")
        self.assertEqual(config["environments"]["check"]["solve-group"], "firmware")
        self.assertIn("gcc", config["feature"]["build"]["dependencies"])
        self.assertNotIn("gh", config["dependencies"])
        self.assertNotIn("setuptools", config["dependencies"])
        self.assertNotIn("pyelftools", config["dependencies"])
        activation = config["feature"]["build"]["activation"]["env"]
        self.assertEqual(activation["ARM_TOOLCHAIN_DIR"], "$PIXI_PROJECT_ROOT/work/cross-15.2.0")
        self.assertIn("$PIXI_PROJECT_ROOT/work/cross-15.2.0/arm-linux-gnueabi/bin", activation["PATH"])
        tasks = config["feature"]["build"]["tasks"]
        self.assertEqual(tasks["build"]["depends-on"], ["prepare"])
        self.assertEqual(tasks["prepare"]["depends-on"], ["setup"])
        self.assertNotIn("build", config["tasks"])
        self.assertFalse((ROOT / "mise.toml").exists())

    def test_setup_verifies_and_reuses_a_cross_compiler(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "with space"
            root.mkdir()
            shutil.copytree(ROOT / "scripts", root / "scripts")
            toolchain = root / "toolchain/arm-linux-gnueabi/bin"
            toolchain.mkdir(parents=True)
            compiler = toolchain / "arm-linux-gnueabi-gcc"
            compiler.write_text("#!/bin/sh\n[ \"$1\" = -dumpmachine ] && { echo arm-linux-gnueabi; exit; }\nwhile [ \"$#\" -gt 0 ]; do [ \"$1\" = -o ] && { shift; : > \"$1\"; exit; }; shift; done\n")
            compiler.chmod(0o755)
            archive = root / "work/gcc-test.tar.xz"
            archive.parent.mkdir()
            with tarfile.open(archive, "w:xz") as output:
                output.add(root / "toolchain", arcname="toolchain")
            env = {**os.environ,
                   "ARM_TOOLCHAIN_URL": "https://example.invalid/gcc-test.tar.xz",
                   "ARM_TOOLCHAIN_SHA256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                   "ARM_TOOLCHAIN_ARCHIVE": "work/gcc-test.tar.xz",
                   "ARM_TOOLCHAIN_DIR": "work/cross-test"}
            for _ in range(2):
                result = subprocess.run(["bash", "scripts/setup.sh"], cwd=root, env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "work/cross-test/arm-linux-gnueabi/bin/arm-linux-gnueabi-gcc").is_file())

class BuildTests(unittest.TestCase):
    def test_u_boot_build_expands_and_enforces_recovery_limit(self):
        script = (ROOT / "scripts/build.sh").read_text()
        self.assertIn("--set-val SYS_BOOTM_LEN 0x1000000", script)
        self.assertIn("'CONFIG_SYS_BOOTM_LEN=0x1000000'", script)

    def test_u_boot_build_keeps_phy_diagnostics_and_waits_for_link(self):
        script = (ROOT / "scripts/build.sh").read_text()
        self.assertIn("--set-val PHY_ANEG_TIMEOUT 15000", script)
        self.assertIn("--enable CMD_MII", script)
        self.assertIn("--enable CMD_MDIO", script)
        self.assertIn("--enable CMD_PING", script)
        self.assertIn("'CONFIG_PHY_ANEG_TIMEOUT=15000'", script)
        self.assertIn("CMD_MII=y CMD_MDIO=y CMD_PING=y", script)

    def test_patch_digest_is_path_independent_and_stale_sources_refresh(self):
        script = (ROOT / "scripts/build.sh").read_text()
        self.assertNotIn('patch_hash=$(sha256sum "$@"', script)
        self.assertIn('sha256sum "$patch" | cut', script)
        self.assertIn('scripts/sources.py refresh "$component"', script)

    def test_openwrt_relocation_discards_only_generated_state(self):
        generated = ("bin", "build_dir", "feeds", "logs", "staging_dir", "tmp")
        for name in generated:
            directory = self.source / name
            directory.mkdir(parents=True)
            (directory / "stale").write_text("old absolute path")
        downloads = self.source / "dl"
        downloads.mkdir()
        (downloads / "preserved").write_text("cached download")
        (self.source / ".q703-build-root").write_text("/tmp/old-checkout\n")

        result = self.build()

        self.assertEqual(result.returncode, 0, result.stderr)
        for name in generated:
            self.assertFalse((self.source / name / "stale").exists())
        self.assertEqual((downloads / "preserved").read_text(), "cached download")
        effective_root = self.build_mount if self.namespace_mode else self.root
        self.assertEqual(
            (self.source / ".q703-build-root").read_text(),
            f"{effective_root}\n",
        )

        retained = self.source / "tmp/retained"
        retained.parent.mkdir()
        retained.write_text("same root")
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(retained.read_text(), "same root")

    def remove_build_mount(self):
        if self.build_mount.is_symlink():
            self.build_mount.unlink()
        elif self.build_mount.is_dir():
            try:
                self.build_mount.rmdir()
            except OSError:
                pass

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="firmware-")
        self.addCleanup(self.tmp.cleanup)
        self.namespace_mode = os.environ.get("FIRMWARE_TEST_SPACED_ROOT") == "1"
        self.root = Path(self.tmp.name)
        if self.namespace_mode:
            self.root /= "checkout with space"
            self.root.mkdir()
            build_id = hashlib.sha256(os.fsencode(self.root.resolve()) + b"\0").hexdigest()
            self.build_mount = Path("/tmp") / f"q703-firmware-{os.geteuid()}-{build_id}"
            self.assertFalse(os.path.lexists(self.build_mount), self.build_mount)
            self.addCleanup(self.remove_build_mount)
        for name in ("scripts", "board", "openwrt", "patches", "keys"):
            shutil.copytree(ROOT / name, self.root / name)
        self.source = self.root / "work/openwrt"
        for directory in ("target/linux/kirkwood/image", "target/linux/kirkwood/patches-6.12", "package/utils/mdadm"):
            (self.source / directory).mkdir(parents=True)
        (self.source / ".source").write_text("a" * 64 + "\n")
        (self.source / "target/linux/kirkwood/Makefile").write_text("KERNEL_PATCHVER:=6.12\n")
        (self.source / "target/linux/kirkwood/config-6.12").write_text("CONFIG_SATA_MV=m\n")
        (self.source / "target/linux/kirkwood/image/Makefile").write_text("$(eval $(call BuildImage))\n")
        (self.source / "package/utils/mdadm/Makefile").write_text("DEPENDS:=+libpthread +kmod-md-mod +kmod-md-raid0 +kmod-md-raid10 +kmod-md-raid1\n")
        (self.root / "work/sources.json").write_text(json.dumps({"openwrt": {
            "sha256": "a" * 64, "epoch": 1, "feeds": {}}}))
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(self.root / "work/key")], check=True)
        subprocess.run(["openssl", "pkey", "-in", str(self.root / "work/key"), "-pubout", "-out", str(self.root / "work/key.pub")], check=True)
        self.env = {**os.environ, "PREPARE_ONLY": "1", "GITHUB_REPOSITORY": "owner/firmware",
                    "RELEASE_PUBKEY": str(self.root / "work/key.pub")}

    def test_committed_release_key_is_the_default_for_ram_builds(self):
        self.env.pop("RELEASE_PUBKEY")
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.source / "files/etc/firmware/release.pub").read_bytes(),
            (self.root / "keys/release.pub").read_bytes(),
        )

    def build(self, *args):
        if not args:
            args = ("openwrt",)
        return subprocess.run(["bash", "scripts/build.sh", *args], cwd=self.root,
                              env=self.env, capture_output=True, text=True)

    def test_preparation_is_repeatable_and_embeds_updater(self):
        for _ in range(2):
            result = self.build()
            self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.source / "target/linux/kirkwood/config-6.12").read_text()
        self.assertEqual(config.count("CONFIG_SATA_MV=y"), 1)
        self.assertNotIn("kmod-md-", (self.source / "package/utils/mdadm/Makefile").read_text())
        self.assertEqual((self.source / "target/linux/kirkwood/image/Makefile").read_text().count("include ./ts221.mk"), 1)
        self.assertTrue(os.access(self.source / "files/usr/sbin/firmware-update", os.X_OK))
        self.assertTrue(os.access(self.source / "files/usr/sbin/firmware-install", os.X_OK))
        self.assertEqual((self.source / "files/etc/firmware/boot.env").read_bytes(),
                         (self.root / "board/boot.env").read_bytes())
        self.assertEqual((self.source / "files/etc/firmware/repository").read_text(), "owner/firmware\n")
        image_config = (self.source / ".config").read_text()
        self.assertIn("CONFIG_PACKAGE_ts221=y", image_config)
        package = self.source / "package/ts221"
        self.assertIn("$(eval $(call BuildPackage,ts221))", (package / "Makefile").read_text())
        self.assertTrue((package / "files/ts221.init").is_file())
        self.assertFalse((self.source / "package/q703").exists())
        for device in ("fujitsu_q703", "qnap_ts221"):
            self.assertIn(f"CONFIG_TARGET_DEVICE_kirkwood_generic_DEVICE_{device}=y", image_config)
        self.assertIn("CONFIG_TARGET_MULTI_PROFILE=y", image_config)
        self.assertIn("# CONFIG_TARGET_PER_DEVICE_ROOTFS is not set", image_config)
        for model in ("q703", "ts221"):
            self.assertFalse((self.root / f"dist/openwrt-{model}.tar.gz").exists())

    def test_changed_lock_refuses_mixed_outputs(self):
        self.assertEqual(self.build().returncode, 0)
        (self.root / "dist/sources.json").write_text("{}")
        self.assertNotEqual(self.build().returncode, 0)

    def test_one_openwrt_build_packages_both_models(self):
        self.env.pop("PREPARE_ONLY")
        self.env["JOBS"] = "2"
        effective_root = self.build_mount if self.namespace_mode else self.root.resolve()
        (self.source / ".q703-build-root").write_text(str(effective_root) + "\n")
        scripts = self.source / "scripts"
        scripts.mkdir()
        (scripts / "feeds").write_text("#!/bin/sh\nexit 0\n")
        (scripts / "feeds").chmod(0o755)
        fakebin = self.root / "bin"
        fakebin.mkdir()
        calls = self.root / "make.calls"
        (fakebin / "make").write_text(
            f'#!/bin/sh\nprintf "%s|%s|%s|%s|%s\\n" "${{CC-unset}}" '
            f'"${{CROSS_COMPILE-unset}}" "${{ARCH-unset}}" "$PWD" "$*" >> "{calls}"\n')
        (fakebin / "make").chmod(0o755)
        self.env["PATH"] = f"{fakebin}:{self.env['PATH']}"
        self.env.update({"CC": "host-cc", "CROSS_COMPILE": "host-cross-", "ARCH": "host"})
        output = self.source / "bin/targets/kirkwood/generic"
        output.mkdir(parents=True)
        prefix = "openwrt-test-kirkwood-generic"
        rootfs_buffer = io.BytesIO()
        with tarfile.open(fileobj=rootfs_buffer, mode="w") as rootfs_archive:
            member = tarfile.TarInfo("./etc/mke2fs.conf")
            member.size = 2
            rootfs_archive.addfile(member, io.BytesIO(b"[\n"))
        rootfs_bytes = gzip.compress(rootfs_buffer.getvalue())
        (output / f"{prefix}-rootfs.tar.gz").write_bytes(rootfs_bytes)
        (output / f"{prefix}.manifest").write_text("".join(
            f"{package} - 1\n" for package in
            ("ts221", "mdadm", "curl", "ca-bundle", "openssl-util", "jsonfilter", "flock",
             "blockdev", "uboot-envtools", "mtd", "e2fsprogs")))
        for model, device in (("q703", "fujitsu_q703"), ("ts221", "qnap_ts221")):
            (output / f"{prefix}-{device}-initramfs-uImage").write_bytes(f"recovery {model}".encode())
        kernel = self.source / "build_dir/target-arm/linux-kirkwood_generic/linux-6.12"
        kernel.mkdir(parents=True)
        shutil.copyfile(self.root / "board/kernel.config", kernel / ".config")
        image_dir = kernel.parent
        for model, device in (("q703", "fujitsu_q703"), ("ts221", "qnap_ts221")):
            (image_dir / f"{device}-uImage").write_bytes(f"kernel {model}".encode())

        for _ in range(2):
            result = self.build()
            self.assertEqual(result.returncode, 0, result.stderr)

        fields = ("cc", "cross_compile", "arch", "pwd", "arguments")
        records = [
            dict(zip(fields, line.split("|", 4)))
            for line in calls.read_text().splitlines()
        ]
        self.assertEqual(len(records), 4)
        for record in records:
            self.assertEqual(
                (record["cc"], record["cross_compile"], record["arch"]),
                ("unset", "unset", "unset"),
            )

        defconfigs = [record for record in records if record["arguments"] == "defconfig"]
        builds = [record for record in records if record["arguments"].startswith("-C ")]
        self.assertEqual(len(defconfigs), 2)
        self.assertEqual(len(builds), 2)
        self.assertEqual(len(defconfigs) + len(builds), len(records))
        effective_roots = {record["pwd"] for record in builds}
        self.assertEqual(len(effective_roots), 1)
        effective_root = effective_roots.pop()
        self.assertFalse(any(character.isspace() for character in effective_root))
        for record in defconfigs:
            self.assertEqual(record["pwd"], f"{effective_root}/work/openwrt")
        for record in builds:
            arguments = record["arguments"].split()
            self.assertEqual(arguments[0], "-C")
            self.assertEqual(arguments[2], "-j2")
            self.assertEqual(arguments[1], f"{effective_root}/work/openwrt")
            self.assertFalse(any(character.isspace() for character in arguments[1]))
            if self.namespace_mode:
                self.assertIn("FAKEROOT=bwrap", record["arguments"])
                self.assertIn("--unshare-user --uid 0 --gid 0", record["arguments"])
                self.assertTrue(record["arguments"].endswith(
                    f"{effective_root}/work/openwrt/staging_dir/host/bin/fakeroot"
                ))
            else:
                self.assertEqual(len(arguments), 3)

        if self.namespace_mode:
            message_prefix = "Checkout path contains whitespace; building through "
            aliases = [
                Path(line[len(message_prefix):-1])
                for line in result.stderr.splitlines()
                if line.startswith(message_prefix) and line.endswith(".")
            ]
            self.assertEqual(aliases, [self.build_mount])
            self.assertFalse(self.build_mount.exists())
        for model in ("q703", "ts221"):
            with tarfile.open(self.root / f"dist/openwrt-{model}.tar.gz") as archive:
                self.assertEqual(archive.extractfile("./kernel.uImage").read(), f"kernel {model}".encode())
                self.assertEqual(archive.extractfile("./recovery.uImage").read(), f"recovery {model}".encode())
                self.assertEqual(archive.extractfile("./rootfs.tar.gz").read(), rootfs_bytes)
        self.assertEqual((self.root / "dist/release.pub").read_bytes(),
                         (self.root / "work/key.pub").read_bytes())
        for model, device in (("q703", "fujitsu_q703"), ("ts221", "qnap_ts221")):
            image = image_dir / f"{device}-uImage"
            image.unlink()
            self.assertNotEqual(self.build().returncode, 0)
            self.assertFalse((self.root / f"dist/openwrt-{model}.tar.gz").exists())
            image.write_bytes(f"kernel {model}".encode())

        rootfs_buffer = io.BytesIO()
        with tarfile.open(fileobj=rootfs_buffer, mode="w") as rootfs_archive:
            member = tarfile.TarInfo("./etc/other.conf")
            member.size = 0
            rootfs_archive.addfile(member, io.BytesIO(b""))
        (output / f"{prefix}-rootfs.tar.gz").write_bytes(gzip.compress(rootfs_buffer.getvalue()))
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing required rootfs file: /etc/mke2fs.conf", result.stderr)
        self.assertFalse((self.root / "dist/openwrt-q703.tar.gz").exists())

    def test_invalid_component_reports_usage(self):
        result = self.build("invalid")
        self.assertEqual(result.returncode, 2)
        self.assertIn("build: u-boot|openwrt|linux", result.stderr)

    def test_spaced_launcher_keeps_the_outer_build_unprivileged(self):
        with (
            tempfile.TemporaryDirectory(prefix="firmware checkout with space ") as tmp,
            tempfile.TemporaryDirectory(prefix="firmware-fakebin-") as fakebin_tmp,
        ):
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copyfile(ROOT / "scripts/build.sh", scripts / "build.sh")
            fakebin = Path(fakebin_tmp)
            calls = root / "bwrap.calls"
            launcher = fakebin / "bwrap"
            launcher.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$BWRAP_CALLS"\nexit 99\n')
            launcher.chmod(0o755)
            env = {
                **os.environ,
                "BWRAP_CALLS": str(calls),
                "PATH": f"{fakebin}:{os.environ['PATH']}",
            }

            result = subprocess.run(
                ["bash", "scripts/build.sh", "openwrt"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 99)
            arguments = calls.read_text().splitlines()
            self.assertIn("--unshare-user", arguments)
            self.assertIn("--unshare-pid", arguments)
            self.assertNotIn("--uid", arguments)
            self.assertNotIn("--gid", arguments)
            build_id = hashlib.sha256(os.fsencode(root.resolve()) + b"\0").hexdigest()
            build_mount = Path("/tmp") / f"q703-firmware-{os.geteuid()}-{build_id}"
            self.assertFalse(build_mount.exists())

    def test_namespace_rejects_symlinked_build_mount_before_mutation(self):
        if not self.namespace_mode:
            self.skipTest("requires FIRMWARE_TEST_SPACED_ROOT=1")

        fakebin = self.root / "bin"
        fakebin.mkdir()
        launcher_calls = self.root / "bwrap.calls"
        make_calls = self.root / "make.calls"
        (fakebin / "bwrap").write_text(
            f'#!/bin/sh\nprintf "bwrap\\n" >> "{launcher_calls}"\nexit 99\n'
        )
        (fakebin / "make").write_text(
            f'#!/bin/sh\nprintf "make\\n" >> "{make_calls}"\nexit 99\n'
        )
        for executable in (fakebin / "bwrap", fakebin / "make"):
            executable.chmod(0o755)
        self.env["PATH"] = f"{fakebin}:{self.env['PATH']}"
        self.env.pop("PREPARE_ONLY")
        self.build_mount.symlink_to(self.root, target_is_directory=True)

        result = self.build()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"Unsafe build mount path: {self.build_mount}", result.stderr)
        self.assertFalse((self.root / "dist").exists())
        self.assertFalse(launcher_calls.exists())
        self.assertFalse(make_calls.exists())

    def test_patch_files_parse(self):
        for patch in (ROOT / "patches").rglob("*.patch"):
            result = subprocess.run(["git", "apply", "--numstat", str(patch)],
                                    cwd=self.root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.strip(), patch)
