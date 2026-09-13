# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/recovery_preflight.py"
SPEC = importlib.util.spec_from_file_location("recovery_preflight", SCRIPT)
assert SPEC and SPEC.loader
PREFLIGHT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREFLIGHT)


class RecoveryPreflightTests(unittest.TestCase):
    def test_stages_read_only_uboot_copy_with_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.kwb"
            destination = root / "staged.kwb"
            image = b"kwboot candidate"
            source.write_bytes(image)

            size, digest = PREFLIGHT.stage_uboot(source, destination)

            self.assertEqual(destination.read_bytes(), image)
            self.assertEqual(size, len(image))
            self.assertEqual(digest, hashlib.sha256(image).hexdigest())
            self.assertEqual(destination.stat().st_mode & 0o777, 0o444)

    def test_rejects_uboot_larger_than_nor_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.kwb"
            source.write_bytes(b"x" * (512 * 1024 + 1))

            with self.assertRaisesRegex(SystemExit, "exceeds 512 KiB"):
                PREFLIGHT.stage_uboot(source, root / "staged.kwb")

            self.assertFalse((root / "staged.kwb").exists())
