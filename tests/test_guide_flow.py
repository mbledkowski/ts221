# SPDX-License-Identifier: AGPL-3.0-or-later
"""Execute the guide's actual shell blocks with controlled failing commands."""

from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def block_containing(document, marker):
    blocks = re.findall(r"^[ \t]*```sh\n(.*?)^[ \t]*```[ \t]*$",
                        (ROOT / document).read_text(), re.S | re.M)
    return next(block for block in blocks if marker in block)


class InstallBuildFlow(unittest.TestCase):
    def run_build(self, fail="", locked=False, selected=True):
        block = block_containing("docs/install.md", "HOST_BUILD_OK=0")
        with tempfile.TemporaryDirectory() as temporary:
            if locked:
                work = Path(temporary) / "work"
                work.mkdir()
                (work / "sources.json").write_text("{}")
            script = r'''
pixi() {
    shift
    case "$1" in
        python) stage=lock-check ;;
        build) stage="build-$2" ;;
        *) stage=$1 ;;
    esac
    printf 'CALL:%s\n' "$stage"
    test "$stage" != "$FAIL_STAGE"
}
python3() { printf 'CALL:lock-check\n'; test lock-check != "$FAIL_STAGE"; }
''' + block + '\nprintf "ALIVE:%s\\n" "$HOST_BUILD_OK"\n'
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-c", script],
                cwd=temporary, text=True, capture_output=True,
                env={"PATH": "/usr/bin:/bin", "FAIL_STAGE": fail,
                     "BUILD_SELECTION_OK": str(int(selected))},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = re.findall(r"^CALL:(.*)$", result.stdout, re.M)
        return calls, result

    def test_every_failed_stage_stops_without_closing_shell(self):
        stages = ["resolve", "lock-check", "prepare", "build-u-boot",
                  "build-openwrt", "build-linux", "test", "lint"]
        for index, stage in enumerate(stages):
            with self.subTest(stage=stage):
                calls, result = self.run_build(fail=stage)
                self.assertEqual(calls, stages[:index + 1])
                self.assertIn("ALIVE:0", result.stdout)
                self.assertNotIn("Complete host build and checks passed.", result.stdout)
                self.assertIn("STOP:", result.stderr)

    def test_success_requires_all_selected_stages(self):
        for locked in (False, True):
            with self.subTest(locked=locked):
                calls, result = self.run_build(locked=locked)
                expected = ([] if locked else ["resolve"])
                expected += ["lock-check", "prepare", "build-u-boot",
                             "build-openwrt", "build-linux", "test", "lint"]
                self.assertEqual(calls, expected)
                self.assertIn("ALIVE:1", result.stdout)

    def test_invalid_selection_runs_nothing(self):
        calls, result = self.run_build(selected=False)
        self.assertEqual(calls, [])
        self.assertIn("ALIVE:0", result.stdout)


class GuideFailureGuards(unittest.TestCase):
    def run_shell(self, script, stdin=""):
        return subprocess.run(["bash", "--noprofile", "--norc", "-c", script],
                              input=stdin, text=True, capture_output=True)

    def test_session_creation_failure_does_not_install_trap_or_touch_paths(self):
        block = block_containing("docs/install.md", "start_recovery_session()")
        result = self.run_shell('''
unset SESSION
mktemp() { return 1; }
mkdir() { echo UNEXPECTED_MKDIR; }
chmod() { echo UNEXPECTED_CHMOD; }
''' + block + '\nprintf "ALIVE:%s\\n" "$SESSION_READY"\ntrap -p EXIT\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ALIVE:0", result.stdout)
        self.assertNotIn("UNEXPECTED", result.stdout)
        self.assertNotIn("trap --", result.stdout)

    def test_firewall_inspection_failure_preserves_rollback_record(self):
        block = block_containing("docs/install.md", "start_recovery_session()")
        # Define the session functions without executing initialization.
        definitions = block.split("if start_recovery_session; then")[0]
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            record = session / "firewall.rollback"
            record.write_text("preserve this")
            for status in (1, 2, 252):
                with self.subTest(status=status):
                    # Nested cleanup functions are defined when setup is run; override
                    # creation and trap registration, so no host state is changed.
                    result = self.run_shell('''
unset SESSION
mktemp() { printf '%s\\n' "$TEST_SESSION"; }
mkdir() { :; }
chmod() { :; }
trap() { :; }
''' + f"TEST_SESSION='{session}'\n" + definitions + f'''
start_recovery_session || exit 90
FW_RULE_ADDED=1
FW_RULE=example
FW_ZONE=public
sudo() {{ return {status}; }}
if close_tftp_firewall; then echo UNEXPECTED_SUCCESS; fi
printf 'ADDED:%s\\n' "$FW_RULE_ADDED"
''')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("UNEXPECTED_SUCCESS", result.stdout)
                    self.assertIn("ADDED:1", result.stdout)
                    self.assertEqual(record.read_text(), "preserve this")

    def test_tftp_command_requires_assembly_and_working_python(self):
        block = block_containing("docs/install.md", "print_tftp_command()")
        for assembled in (0, 1):
            result = self.run_shell(f"TFTP_ASSEMBLED={assembled}\npixi() {{ return 1; }}\n"
                                    + block + '\necho ALIVE\n')
            self.assertIn("STOP:", result.stderr)
            self.assertNotIn("sudo ", result.stdout)
            self.assertIn("ALIVE", result.stdout)

    def test_firewall_assembly_stops_on_inspection_errors(self):
        block = block_containing("docs/install.md", "assemble_tftp_host()")
        for failure in ("state", "list-rich-rules"):
            with self.subTest(failure=failure):
                result = self.run_shell(f'''
RECOVERY_PREFLIGHT_OK=1
NETWORK_OK=1
firewall-cmd() {{ :; }}
sudo() {{
    case "$*" in
        *--{failure}*) return 1 ;;
        ss*) return 0 ;;
        *--state*) echo running ;;
        *--get-zone-of-interface*) echo public ;;
        *) echo UNEXPECTED_MUTATION >&2; return 99 ;;
    esac
}}
''' + block + '\nprintf "ALIVE:%s\\n" "$TFTP_ASSEMBLED"\n')
                self.assertIn("ALIVE:0", result.stdout)
                self.assertIn("STOP:", result.stderr)
                self.assertNotIn("UNEXPECTED", result.stderr)

    def test_cleanup_handles_partial_setup_and_repeated_calls(self):
        block = block_containing("docs/install.md", "start_recovery_session()")
        definitions = block.split("if start_recovery_session; then")[0]
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "session"
            session.mkdir()
            result = self.run_shell(f'''
unset SESSION
mktemp() {{ printf '%s\\n' '{session}'; }}
mkdir() {{ return 1; }}
trap() {{ :; }}
''' + definitions + '''
if start_recovery_session; then echo UNEXPECTED_SETUP; fi
cleanup_session && cleanup_session && echo CLEANED
''')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("UNEXPECTED", result.stdout)
            self.assertIn("CLEANED", result.stdout)
            self.assertFalse(session.exists())

    def test_firewall_removal_and_recheck_errors_preserve_record(self):
        block = block_containing("docs/install.md", "start_recovery_session()")
        definitions = block.split("if start_recovery_session; then")[0]
        for failure in ("remove", "recheck"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                record = Path(temporary) / "firewall.rollback"
                record.write_text("preserve this")
                result = self.run_shell(f'''
unset SESSION
mktemp() {{ printf '%s\\n' '{temporary}'; }}
mkdir() {{ :; }}
chmod() {{ :; }}
trap() {{ :; }}
''' + definitions + f'''
start_recovery_session || exit 90
FW_RULE_ADDED=1
FW_RULE=example
FW_ZONE=public
FW_REMOVED=0
sudo() {{
    case "$*" in
        *--list-rich-rules*)
            if test "$FW_REMOVED" -eq 1; then return 1; fi
            printf '%s\\n' "$FW_RULE" ;;
        *--remove-rich-rule*)
            test '{failure}' != remove || return 1
            FW_REMOVED=1 ;;
        *) return 99 ;;
    esac
}}
if close_tftp_firewall; then echo UNEXPECTED_SUCCESS; fi
printf 'ADDED:%s\\n' "$FW_RULE_ADDED"
''')
                self.assertNotIn("UNEXPECTED", result.stdout)
                self.assertIn("ADDED:1", result.stdout)
                self.assertTrue(record.exists())

    def test_manifest_copy_failure_does_not_transfer_stale_files(self):
        block = block_containing("docs/install.md", "transfer_release()")
        for failed_copy in (1, 2):
            result = self.run_shell(f'''
SIGNED_RELEASE_OK=1
count=0
cp() {{ count=$((count + 1)); test "$count" -ne {failed_copy}; }}
tar() {{ echo UNEXPECTED_TAR >&2; }}
ssh() {{ echo UNEXPECTED_SSH >&2; }}
''' + block + '\necho ALIVE\n')
            self.assertIn("STOP:", result.stderr)
            self.assertNotIn("UNEXPECTED", result.stderr)
            self.assertIn("ALIVE", result.stdout)

    def test_invalid_update_inputs_leave_shell_open_and_gate_closed(self):
        for marker, value, flag in [
            ("select_update_key()", "relative.pem", "RELEASE_KEY_OK"),
            ("select_update_repository()", "owner/name", "REPOSITORY_OK"),
            ("select_update_repository()", "not a/repository", "REPOSITORY_OK"),
        ]:
            with self.subTest(value=value):
                block = block_containing("docs/update.md", marker)
                result = self.run_shell(block + f'\nprintf "ALIVE:%s\\n" "${flag}"\n',
                                        value + "\n")
                self.assertEqual(result.returncode, 0)
                self.assertIn("ALIVE:0", result.stdout)
                self.assertIn("STOP:", result.stderr)

    def test_all_documented_shell_blocks_parse(self):
        for document in ("docs/install.md", "docs/update.md"):
            blocks = re.findall(r"^[ \t]*```sh\n(.*?)^[ \t]*```[ \t]*$",
                                (ROOT / document).read_text(), re.S | re.M)
            for index, block in enumerate(blocks):
                with self.subTest(document=document, block=index):
                    result = subprocess.run(["bash", "-n"], input=block,
                                            text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
