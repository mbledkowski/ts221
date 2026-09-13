# SPDX-License-Identifier: GPL-2.0-or-later
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BootTests(unittest.TestCase):
    # Execute the portable shell commands with fake U-Boot primitives, not hardware.
    def boot(self, command="bootcmd", **state):
        environment = dict(line.split("=", 1) for line in
                           (ROOT / "board/boot.env").read_text().splitlines())
        script = r'''
setenv() { name=$1; shift; export "$name=$*"; }
run() { eval "${!1}"; }
sleep() { :; }
sata() {
    case "$1" in
    init) test "${fail_init:-0}" = 0 ;;
    device) test "$2" != "${fail_disk:-none}" ;;
    read) printf 'read %s %s %s %s %s\n' "$slot" "$disk" "$2" "$3" "$4"
          test "$slot:$disk" != "${fail_read:-none}" ;;
    esac
}
iminfo() { test "$slot:$disk" != "${fail_image:-none}"; }
bootm() {
    printf 'boot %s %s %s\n' "$slot" "$disk" "$bootargs"
    if test "$slot:$disk" = "${success:-none}"; then exit 77; fi
    return 1
}
run "$1"
'''
        return subprocess.run(["bash", "-c", script, "boot-test", command],
                              env={**os.environ, **environment, **state},
                              capture_output=True, text=True)

    def test_each_slot_uses_its_root_and_raw_kernel(self):
        for slot, lba, root in (("A", "0x800", "md0"), ("B", "0x20800", "md1")):
            result = self.boot(active_slot=slot, success=f"{slot}:0")
            self.assertEqual(result.returncode, 77, result.stderr)
            lines = result.stdout.splitlines()
            self.assertEqual(lines[0], f"read {slot} 0 0x00800000 {lba} 0x8000")
            self.assertIn(f"firmware.layout=1 firmware.slot={slot}", lines[1])
            self.assertIn(f"root=/dev/{root} rootwait", lines[1])
            self.assertIn("raid=autodetect panic=10", lines[1])

    def test_returning_bootm_tries_other_disk_then_rollback(self):
        result = self.boot(active_slot="B", rollback_slot="A", success="A:0")
        boots = [line.split()[1:3] for line in result.stdout.splitlines() if line.startswith("boot ")]
        self.assertEqual(boots, [["B", "0"], ["B", "1"], ["A", "0"]])
        self.assertEqual(result.returncode, 77)

    def test_failed_read_and_invalid_image_are_never_booted(self):
        for failure in ("fail_read", "fail_image"):
            result = self.boot(active_slot="A", success="A:1", **{failure: "A:0"})
            self.assertNotIn("boot A 0", result.stdout)
            self.assertIn("boot A 1", result.stdout)
            self.assertEqual(result.returncode, 77)

    def test_missing_disk_tries_replica(self):
        result = self.boot(active_slot="B", fail_disk="0", success="B:1")
        self.assertNotIn("read B 0", result.stdout)
        self.assertIn("boot B 1", result.stdout)
        self.assertEqual(result.returncode, 77)

    def test_bootlimit_path_only_tries_rollback(self):
        result = self.boot("altbootcmd", active_slot="B", rollback_slot="A", success="A:1")
        self.assertNotIn("read B", result.stdout)
        self.assertIn("boot A 1", result.stdout)
        self.assertEqual(result.returncode, 77)

    def test_invalid_slot_never_reuses_previous_lba(self):
        result = self.boot(active_slot="bad", rollback_slot="A", lba="0x20800", success="A:0")
        self.assertNotIn("read bad", result.stdout)
        self.assertEqual(result.returncode, 77)

    def test_failed_sata_initialization_never_reads(self):
        self.assertEqual(self.boot(fail_init="1").stdout, "")

    def test_environment_fits_and_never_saves_unconfirmed_state(self):
        environment = (ROOT / "board/boot.env").read_text()
        self.assertLess(len(environment.encode()), 3000)  # Leave room for U-Boot defaults.
        self.assertNotIn("saveenv", environment)
        self.assertIn("bootlimit=3\n", environment)
        self.assertIn("upgrade_available=0\n", environment)
