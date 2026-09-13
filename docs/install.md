# Install OpenWrt or test RAM recovery

Use this runbook for a Fujitsu CELVIN Q703 or QNAP TS-221 NAS. It guides you
through building the firmware, testing a temporary rescue system in RAM,
and—if you choose—installing it permanently.

After completing this guide, you will be able to:

- Build and validate firmware for the selected model.
- Boot and verify the recovery system in RAM before intended storage changes.
- Install signed OpenWrt using explicit disk, backup, and NOR gates.

Read the entire guide before starting. Work through the numbered steps in order.
If a check fails before persistent writes, stop at that step and follow
[Cleanup](#cleanup). If a storage write fails during or after steps 9–11,
retain the RAM session, UART logs, and backups; do not reboot as routine cleanup.
A build that finishes successfully does not prove that the firmware works on
your hardware: the RAM recovery test on your target model must pass before any
permanent installation.

Run all repository commands from the repository root (the directory containing
`pixi.toml`, `scripts/`, and `keys/`). Paths such as `dist/` and `work/` refer
to the repository root.

## Glossary

- **H, T, U:** Labels for three terminal windows on your build host:
  - **H:** Preparation, preflight checks, SSH connection, and cleanup.
  - **T:** Foreground temporary TFTP server.
  - **U:** `kwboot` serial transfer, then the live NAS serial console.
- **U-Boot:** The bootloader, which starts before Linux.
- **NOR:** The NAS's built-in 16 MiB SPI flash chip. It holds the single bootloader.
- **RAM recovery:** A temporary rescue system loaded into NAS memory. Running in
  RAM does not, by itself, prevent software from writing to attached disks.
- **Slot A / Slot B:** Two mirrored RAID1 arrays (`md0` and `md1`) holding
  independent system copies for updates and rollback.
- **Manifest:** A signed file identifying a release, its files, hashes, and sizes.
- **`sysupgrade`:** OpenWrt's generic installer. **Never** use it on this disk layout.

## How the runbook flows

```text
Build → preflight → network/TFTP → RAM U-Boot → RAM Linux checks
                                                   |
                                cleanup ← decision → sign → disks → NOR
```

The corresponding host gates are `SESSION_READY` → `RECOVERY_PREFLIGHT_OK` →
`TFTP_ASSEMBLED` → `TFTP_READY`; the NAS then verifies the RAM root before the
decision point.

Start at [step 1](#1-prepare-artifacts-and-terminals). The decision to stop testing
or proceed with permanent installation comes after
[step 7](#7-verify-ram-root-before-password-or-network-changes). Destructive disk
partitioning begins in [step 9](#9-prepare-system-arrays); bootloader replacement
is in [step 11](#nor-flashing).

Steps 1–7 build firmware files and test U-Boot together with the recovery image
on real hardware. These files are unsigned: you do not need a private key,
manifest, signature, release tag, GitHub account, or published release.

These steps are intended to leave the installed system unchanged. RAM boot alone
does not prevent a Linux service from writing to attached storage. Step 7 checks
that internal filesystems are not mounted before you proceed.

After step 7 passes, choose one of these outcomes:

- **Stop: unsigned RAM recovery test.** Run [cleanup](#cleanup). The box is
  intended to retain its existing installation, and RAM settings vanish at
  power-off. Building still embeds a public trust anchor and the update
  repository identity for possible later use.
- **Continue: local permanent installation.** Steps 8–11 sign the exact
  qualified bytes and replace both system volumes, the redundant boot
  environment, and the single NOR bootloader. This requires the private
  release key matching the embedded anchor, a locally signed manifest, a new
  tag, and verified off-device backups. It does not require GitHub upload.

| | Stop after step 7 | Continue through steps 8–11 |
|---|---|---|
| Extra inputs | None | Private key, external backup disk |
| Intended device changes | Temporary RAM session; verify storage checks | Disks, boot environment, NOR |
| Undo | Power off | Restore from verified backups |
| Result | A proven rescue path | Permanent boot and automatic updates |

> **Warning:** Permanent installation replaces both system volumes and the single
> NOR bootloader. This is not an in-place migration from QTS. Before proceeding,
> make and verify backups of your data, partition tables, RAID metadata, NOR,
> and settings, stored away from the NAS's internal disks. The commands later in
> this guide back up NOR; they do not replace wider backups.

You may install a locally signed release without uploading it anywhere.
Publishing that signed set is a separate, optional procedure in
[the release guide](update.md#draft-publication).

## Recovery safety invariants

Always test the RAM-loaded U-Boot and recovery image as a pair. A recovery
image can exceed U-Boot's common 8 MiB boot limit, so this repository builds
U-Boot with a 16 MiB limit (`CONFIG_SYS_BOOTM_LEN`). The host check in step 2
rejects a pair if the recovery payload will not fit.

Recovery has failed if `bootm` reports `Image too large`, resets the NAS, or
continues into a normal disk boot. Do not mistake an existing OpenWrt system for
the rescue system just because its prompt, model name, or kernel version looks
familiar. Step 7 checks where Linux is running from, which board image is loaded,
and whether any internal filesystems are mounted. Complete those checks before
changing anything in Linux.

---

## 1. Prepare artifacts and terminals

Have OpenSSH, iproute2 (`ip` and `ss`), `sudo`, and standard GNU tools
(coreutils, diffutils, and tar) available on your Linux x86-64 build host.
Pixi supplies Python 3 and build tools; host development packages are not needed.
Complete [Prepare the build computer](../README.md#1-prepare-the-build-computer)
first. Start in the repository root, where `pixi.toml` is located.

### Choose the target board and repository identity

Check these two choices before running the block below:
- Set `MODEL=q703` for a Fujitsu CELVIN Q703 or `MODEL=ts221` for a QNAP TS-221.
- Set `GITHUB_REPOSITORY` to the real, stable GitHub `owner/name` that will
  provide future updates.
  
The repository identity becomes part of OpenWrt. Signing later cannot change it.
Changing it requires rebuilding and retesting every image that contains it.
Use `local/ram-recovery` only if this build will **never** be installed; that
identity cannot be used to publish updates.

**Execution context:** [Host - Terminal H]
```sh
BUILD_SELECTION_OK=0
select_install_build() {
    MODEL=q703                    # Use ts221 only for a QNAP TS-221.
    GITHUB_REPOSITORY=mbledkowski/ts221   # Real owner/name; local/ram-recovery only for never-install runs.
    case "$MODEL" in q703|ts221) ;; *) echo 'STOP: invalid model.' >&2; return 1 ;; esac
    case "$GITHUB_REPOSITORY" in OWNER/REPOSITORY|owner/name|'')
        echo 'STOP: set the real repository identity.' >&2
        return 1
        ;;
    esac
    printf '%s\n' "$GITHUB_REPOSITORY" |
        grep -Eq '^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$' || return
    GITHUB_SHA=$(git rev-parse HEAD) || return
    export MODEL GITHUB_REPOSITORY GITHUB_SHA
}
if select_install_build; then
    BUILD_SELECTION_OK=1
    echo 'Build selection passed.'
else
    echo 'STOP: correct the build selection before continuing.' >&2
fi
```

**Continue only when:** The block prints `Build selection passed.`

### Build the artifacts

The next block builds the complete set and runs checks. On a first build, it
creates `work/sources.json`, which records the selected source versions. If that
file already exists, keep it. Retrying a build is not a reason to select new
upstream versions.

**Execution context:** [Host - Terminal H]
```sh
HOST_BUILD_OK=0
build_install_artifacts() {
    test "${BUILD_SELECTION_OK:-0}" -eq 1 || return 1
    if ! test -s work/sources.json; then
        pixi run resolve || return
    fi
    pixi run python -c 'import json,re; assert re.fullmatch(r"[0-9a-f]{40}", json.load(open("work/sources.json")).get("project_commit", "")), "source lock lacks project_commit; resolve once with GITHUB_SHA set before building"' || return
    pixi run prepare || return
    pixi run build u-boot || return
    pixi run build openwrt || return
    pixi run build linux || return
    pixi run test || return
    pixi run lint || return
}
if build_install_artifacts; then
    HOST_BUILD_OK=1
    export HOST_BUILD_OK
    echo 'Complete host build and checks passed.'
else
    echo 'STOP: host build/checks failed; do not open the NAS session.' >&2
fi
```

**Continue only when:** The final line says `Complete host build and checks passed.`
and each build has printed its expected `Built dist/...` line.

The U-Boot and OpenWrt builds produce files for both models. The Linux build
produces `dist/linux.tar.gz`. That archive is not installed on the NAS, but the
release signer requires it to validate a complete distribution.

If `resolve` reports a DNS or connection error, stop here. It needs access to the
upstream URL shown in the error; fix connectivity and rerun this build block.
Do not continue with `prepare` or builds when resolving fails, and do not delete
an existing source lock to work around a network error.

A rebuild replaces the affected firmware files. Any signatures for old files
become invalid. Unsigned files are fine for the RAM test; permanent installation
needs a freshly signed set matching the tested files.

### Open three terminals

Open three host terminal windows or tabs. Keep all three on the build host; do
not SSH to the NAS yet:

- **H:** Preparation, checks, SSH, and cleanup.
- **T:** Foreground TFTP server.
- **U:** `kwboot`, then the NAS serial console.

In tmux, press `Ctrl-b`, release, then press `c` to create a window; switch
windows with `Ctrl-b` followed by `n` or `p`.

Terminal U starts on the host. After `kwboot` hands over control, U becomes the
NAS console: commands entered there run on the NAS.

### Start the recovery session in H

In H, start a separate Bash shell. Keep this shell open throughout the
procedure: it holds the session settings and cleanup functions.

**Execution context:** [Host - Terminal H]
```sh
bash
```

Wait for the prompt, then paste the next block into that shell. Repeat the model
and repository choices used for the build. A successful setup prints a temporary
session directory.

**Execution context:** [Host - Terminal H]
```sh
set -o pipefail
SESSION_READY=0
start_recovery_session() {
    test -z "${SESSION:-}" || {
        echo 'STOP: a session already exists in this shell; clean it up first.' >&2
        return 1
    }
    MODEL=q703                     # Repeat the choices made above.
    GITHUB_REPOSITORY=mbledkowski/ts221   # Repeat the exact value built into OpenWrt.
    REPO=$PWD
    SESSION=$(mktemp -d /tmp/q703-recovery.XXXXXX) || return
    test -n "$SESSION" && test -d "$SESSION" || return 1
    export MODEL GITHUB_REPOSITORY REPO SESSION
    FW_RULE=
    FW_ZONE=
    FW_RULE_ADDED=0
    close_tftp_firewall() {
        if test "${FW_RULE_ADDED:-0}" -eq 1; then
            FW_RULES=$(sudo firewall-cmd --zone="$FW_ZONE" --list-rich-rules) || return
            if grep -Fxq -- "$FW_RULE" <<< "$FW_RULES"; then
                sudo firewall-cmd --zone="$FW_ZONE" --remove-rich-rule="$FW_RULE" || return
            fi
            FW_RULES=$(sudo firewall-cmd --zone="$FW_ZONE" --list-rich-rules) || return
            if grep -Fxq -- "$FW_RULE" <<< "$FW_RULES"; then
                echo 'STOP: this session firewall rule is still present.' >&2
                return 1
            fi
            rm -f -- "$SESSION/firewall.rollback" || return
            FW_RULE_ADDED=0
        fi
    }
    cleanup_files() {
        test -n "${SESSION:-}" || return 1
        test ! -L "$SESSION" || return 1
        test -e "$SESSION" || return 0
        test -d "$SESSION" || return 1
        chmod u+w -- "$SESSION/u-boot-$MODEL.kwb" 2>/dev/null || true
        rm -f -- "$SESSION/u-boot-$MODEL.kwb" \
            "$SESSION/tftp/recovery.uImage" "$SESSION/readback.uImage" \
            "$SESSION/known_hosts" "$SESSION/known_hosts.old" \
            "$SESSION/kernel.config" "$SESSION/openwrt_release" \
            "$SESSION/manifest.json" "$SESSION/manifest.sig" || return
        if test -d "$SESSION/tftp"; then
            rmdir -- "$SESSION/tftp" || return
        fi
        rmdir -- "$SESSION"
    }
    cleanup_session() {
        close_tftp_firewall || return
        cleanup_files
    }
    trap cleanup_session EXIT
    mkdir "$SESSION/tftp" || return
    chmod 0755 "$SESSION/tftp" || return
    printf 'Session directory: %s\n' "$SESSION"
}
if start_recovery_session; then
    SESSION_READY=1
else
    echo 'STOP: session setup failed; do not continue.' >&2
fi
```

Do not add global `set -e` to this interactive shell: a failed check could close
H and hide the error. Each stage records success explicitly. The shell's EXIT
trap provides a cleanup safeguard.

---

## 2. Validate the image before touching the NAS

In H, run the preflight check. It extracts the recovery image into the TFTP
directory, checks checksums and load addresses, checks U-Boot's image-size
limit, and copies the matching U-Boot into the session directory.

Keep the printed size, CRC, and version information to compare with the NAS
console later.

**Execution context:** [Host - Terminal H]
```sh
RECOVERY_PREFLIGHT_OK=0
recovery_host_preflight() {
    test "${SESSION_READY:-0}" -eq 1 || return 1
    if test -z "${MODEL:-}" ||
       test -z "${GITHUB_REPOSITORY:-}" || test -z "${SESSION:-}"; then
        echo 'STOP: run the step 1 setup in H before this preflight.' >&2
        return 1
    fi
    pixi run python scripts/recovery_preflight.py "$MODEL" "$SESSION" || {
        echo 'STOP: recovery image/U-Boot preflight failed.' >&2
        return 1
    }
    ls -l "$SESSION/u-boot-$MODEL.kwb" || return 1
    sha256sum "$SESSION/u-boot-$MODEL.kwb" || return 1
    ls -l "$SESSION/tftp/recovery.uImage" || return 1
    sha256sum "$SESSION/tftp/recovery.uImage" || return 1

    EMBEDDED_REPOSITORY=$(
        tar -xOzf "dist/openwrt-$MODEL.tar.gz" ./rootfs.tar.gz |
            tar -xzOf - ./etc/firmware/repository
    ) || {
        echo 'STOP: cannot read the update repository from the OpenWrt archive.' >&2
        return 1
    }
    printf 'Embedded update repository: %s\n' "$EMBEDDED_REPOSITORY"
    if test "$EMBEDDED_REPOSITORY" != "$GITHUB_REPOSITORY"; then
        echo 'STOP: OpenWrt embeds a different repository identity.' >&2
        return 1
    fi
    tar -xOzf "dist/openwrt-$MODEL.tar.gz" ./rootfs.tar.gz |
        tar -xzOf - ./etc/firmware/release.pub |
        cmp - dist/release.pub || {
            echo 'STOP: the OpenWrt archive and dist/release.pub use different public keys.' >&2
            return 1
        }
    cmp dist/release.pub keys/release.pub || {
        echo 'STOP: dist/release.pub and keys/release.pub differ.' >&2
        return 1
    }
}
if recovery_host_preflight; then
    RECOVERY_PREFLIGHT_OK=1
    echo 'Host recovery preflight passed.'
else
    RECOVERY_PREFLIGHT_OK=0
    echo 'STOP: H remains open for inspection; do not touch the NAS.' >&2
fi
```

**Continue only when:** The final line says `Host recovery preflight passed.`

Otherwise, resolve the reported host problem and rerun the block. Do not start
TFTP or operate the NAS while `RECOVERY_PREFLIGHT_OK` is `0`.

If you rebuild any artifact during this session, rerun this preflight before
continuing so it restages the session copies. From a successful preflight
through the RAM test, do not rebuild or use the mutable U-Boot under `dist/`:
step 5 loads the read-only staged session copy.

The two `cmp` commands print nothing when public keys match. If the printed
repository differs from your selection, stop before using the NAS: rebuild
OpenWrt with the intended identity and repeat the RAM test.

---

## 3. Choose temporary network addresses

Choose a temporary address for the NAS on the same wired network as the host.
Reserve it using your router's DHCP leases or address-pool settings so another
device will not receive it. Do not rely on a failed ping to decide that an
address is free.

In H, edit `NAS_IP` and `NAS_PREFIX` to match your reservation, then run:

**Execution context:** [Host - Terminal H]
```sh
NETWORK_OK=0
choose_recovery_network() {
    test "${RECOVERY_PREFLIGHT_OK:-0}" -eq 1 || {
        echo 'STOP: step 2 has not passed.' >&2
        return 1
    }
    NAS_IP=192.168.1.108
    NAS_PREFIX=24
    HOST_ROUTE=$(ip -4 route get "$NAS_IP") || return
    HOST_IF=$(printf '%s\n' "$HOST_ROUTE" | sed -n 's/.* dev \([^ ]*\).*/\1/p')
    HOST_IP=$(printf '%s\n' "$HOST_ROUTE" | sed -n 's/.* src \([^ ]*\).*/\1/p')
    test -n "$HOST_IF" && test -n "$HOST_IP" || return
    if printf '%s\n' "$HOST_ROUTE" | grep -qw via; then
        echo 'STOP: choose an address on the directly connected wired subnet.' >&2
        return 1
    fi
    ip -4 address show dev "$HOST_IF" || return
    printf 'NAS=%s/%s HOST=%s INTERFACE=%s\n' \
        "$NAS_IP" "$NAS_PREFIX" "$HOST_IP" "$HOST_IF"
}
if choose_recovery_network; then
    NETWORK_OK=1
    export NAS_IP NAS_PREFIX HOST_IP HOST_IF
    echo 'Temporary network selection passed.'
else
    echo 'STOP: network selection failed; do not start TFTP.' >&2
fi
```

**Continue only when:** You see `Temporary network selection passed.` and
confirm that the printed interface is the wired connection to the NAS.
Keep the printed NAS and host addresses for step 6.

---

## 4. Assemble and verify temporary TFTP

TFTP sends the recovery image to U-Boot. It uses UDP port 69 to begin transfer.

In H, check that port 69 is free and, when firewalld is in use, add at most one
temporary firewall rule. The rule is recorded so cleanup can remove it.

**Execution context:** [Host - Terminal H]
```sh
TFTP_ASSEMBLED=0
assemble_tftp_host() {
    test "${RECOVERY_PREFLIGHT_OK:-0}" -eq 1 &&
        test "${NETWORK_OK:-0}" -eq 1 || {
            echo 'STOP: host preflight and network selection must pass first.' >&2
            return 1
        }
    TFTP_LISTENERS=$(sudo ss -H -lunp 'sport = :69') || return
    if test -n "$TFTP_LISTENERS"; then
        echo 'STOP: UDP port 69 is already occupied:' >&2
        sudo ss -lunp 'sport = :69' >&2
        return 1
    fi
    if command -v firewall-cmd >/dev/null; then
        sudo firewall-cmd --state || {
            echo 'STOP: cannot verify firewalld state; resolve this before starting TFTP.' >&2
            return 1
        }
        FW_ZONE=$(sudo firewall-cmd --get-zone-of-interface="$HOST_IF") || return
        if test -z "$FW_ZONE" || test "$FW_ZONE" = 'no zone'; then
            FW_ZONE=$(sudo firewall-cmd --get-default-zone) || return
        fi
        FW_RULE="rule family=\"ipv4\" source address=\"$NAS_IP/32\" destination address=\"$HOST_IP/32\" port port=\"1-65535\" protocol=\"udp\" accept"
        FW_RULES=$(sudo firewall-cmd --zone="$FW_ZONE" --list-rich-rules) || return
        if grep -Fxq -- "$FW_RULE" <<< "$FW_RULES"; then
            printf 'Preserving existing firewall rule in zone %s: %s\n' \
                "$FW_ZONE" "$FW_RULE"
        else
            sudo firewall-cmd --zone="$FW_ZONE" --add-rich-rule="$FW_RULE" || return
            FW_RULE_ADDED=1
            printf '%s\n%s\n' "$FW_ZONE" "$FW_RULE" \
                > "$SESSION/firewall.rollback" || return
            printf 'Added temporary firewall rule in zone %s: %s\n' \
                "$FW_ZONE" "$FW_RULE"
        fi
    fi
}
if assemble_tftp_host; then
    TFTP_ASSEMBLED=1
    echo 'Temporary TFTP host state assembled.'
else
    echo 'STOP: TFTP host assembly failed.' >&2
fi
```

If port 69 is occupied, inspect the reported PID with `sudo ps -fp PID`. Do not
kill a root process or reconfigure an unrelated service blindly. If UFW,
nftables, or another manager controls the firewall, arrange an equivalent
temporary rule with its manager, including a recorded rollback path.

The repository provides the server as `scripts/tftp_server.py`. It serves only
the staged `recovery.uImage`. In H, generate the start command:

**Execution context:** [Host - Terminal H]
```sh
print_tftp_command() {
    test "${TFTP_ASSEMBLED:-0}" -eq 1 || return 1
    TFTP_PYTHON=$(pixi run python -c 'import sys; print(sys.executable)') || return
    test -x "$TFTP_PYTHON" || return 1
    printf 'sudo %q %q %q\n' "$TFTP_PYTHON" \
        "$REPO/scripts/tftp_server.py" "$SESSION/tftp"
}
print_tftp_command || echo 'STOP: TFTP command preparation failed.' >&2
```

The `printf` command does not start TFTP. It prints one line beginning with
`sudo`. Copy that entire printed line, paste it into **terminal T**, and press
Enter. When `sudo` prompts, enter the build host user's password.

T should print `TFTP root=... listening on 0.0.0.0:69` and remain occupied
without returning to a shell prompt. The server is now running. Leave T open
while completing the TFTP transfer. If the prompt returns immediately, read the
error and stop; the server is not running.

Now verify the local TFTP server from H:

**Execution context:** [Host - Terminal H]
```sh
TFTP_READY=0
verify_local_tftp() {
    test "${TFTP_ASSEMBLED:-0}" -eq 1 || return 1
    TFTP_LISTENERS=$(sudo ss -H -lunp 'sport = :69') || return
    test -n "$TFTP_LISTENERS" || {
        echo 'STOP: nothing is listening on UDP port 69.' >&2
        return 1
    }
    sudo ss -lunp 'sport = :69' || return
    pixi run curl --fail --silent --show-error --max-time 30 \
        --output "$SESSION/readback.uImage" \
        tftp://127.0.0.1/recovery.uImage || return
    cmp "$SESSION/tftp/recovery.uImage" "$SESSION/readback.uImage" || return
    rm "$SESSION/readback.uImage" || return
}
if verify_local_tftp; then
    TFTP_READY=1
    echo 'Local TFTP readback passed; NAS work may begin.'
else
    echo 'STOP: local TFTP verification failed; do not touch the NAS.' >&2
fi
```

**Continue only when:** H shows the Python listener and
`Local TFTP readback passed; NAS work may begin.`

---

## 5. Load U-Boot into RAM and stop autoboot

**Hardware connection:** Use a **3.3 V TTL UART** serial adapter.
- Connect GND to GND, adapter TX to NAS RX, and adapter RX to NAS TX.
- **Leave VCC disconnected.**
- Keep Ethernet connected.
- Close any other serial client on `/dev/ttyUSB0`. Only one process may use the port.

This step requires a cold boot. If Linux is currently running on the NAS, run
`poweroff` and wait for shutdown before turning off power. Never cut power while
disks are being written.

In H, generate the `kwboot` command:

**Execution context:** [Host - Terminal H]
```sh
if test "${TFTP_READY:-0}" -eq 1; then
    printf '%q -t -B 115200 /dev/ttyUSB0 -b %q -p\n' \
        "$REPO/work/u-boot/tools/kwboot" "$SESSION/u-boot-$MODEL.kwb"
else
    echo 'STOP: local TFTP verification has not passed.' >&2
fi
```

Copy the printed command into **terminal U** and run it. Check that it names the
session copy of U-Boot, not a file under `dist/`.

1. When `kwboot` prints `Please reboot the target`, power on the NAS.
2. Wait for the transfer. `kwboot` then becomes the NAS serial console.
3. During `Hit any key to stop autoboot`, press **Space**.
4. At the `Marvell>>` or `=>` prompt, run `help mii`:

**Execution context:** [U-Boot UART - Terminal U]
```text
help mii
```

**Continue only when:** `help mii` displays MII command usage. If it says
`Unknown command`, the wrong U-Boot is running; stop and power off.

> **Warning:** **Never run `saveenv` during the RAM test.**

If you missed the autoboot countdown, power off the NAS cleanly and repeat step 5.
Do not paste commands into an unstopped boot sequence.

---

## 6. Transfer, compare, then boot

At the U-Boot prompt in **terminal U**, enter these commands one at a time.
Replace `ipaddr` with the NAS address and `serverip` with the host address from step 3:

**Execution context:** [U-Boot UART - Terminal U]
```text
setenv ipaddr 192.168.1.108
setenv serverip 192.168.1.110
tftpboot 0x00800000 recovery.uImage
```

Progress hashes (`#`) should advance. The transfer takes roughly 5 minutes.
Occasional `T` timeout markers are harmless if transfer resumes. If U-Boot
returns `Retry count exceeded`, rerun the `tftpboot` command once. If it fails
again, leave U at its prompt and diagnose the network link and host firewall.

When the transfer completes, verify the byte count against step 2, then
calculate the CRC32 checksum in U:

**Execution context:** [U-Boot UART - Terminal U]
```text
crc32 0x00800000 ${filesize}
```

**Continue only when:** The transferred byte count and CRC32 match step 2 exactly.

Keep U at the U-Boot prompt while dismantling the host-side TFTP service.
In **terminal T**, press `Ctrl-C` and wait for `TFTP server stopped`.
In **terminal H**, verify port 69 is closed and roll back the firewall rule:

**Execution context:** [Host - Terminal H]
```sh
TFTP_DISASSEMBLED=0
if ! TFTP_LISTENERS=$(sudo ss -H -lunp 'sport = :69'); then
    echo 'STOP: could not inspect UDP port 69.' >&2
elif test -n "$TFTP_LISTENERS"; then
    echo 'STOP: TFTP still has a listener; return to T and stop it.' >&2
    sudo ss -lunp 'sport = :69' >&2
elif close_tftp_firewall; then
    TFTP_READY=0
    TFTP_ASSEMBLED=0
    TFTP_DISASSEMBLED=1
    echo 'Temporary TFTP host state disassembled.'
else
    echo 'STOP: firewall rollback failed.' >&2
fi
```

**Continue only when:** H prints `Temporary TFTP host state disassembled.`

Now return to **terminal U** and boot the recovery image:

**Execution context:** [U-Boot UART - Terminal U]
```text
setenv bootargs console=ttyS0,115200 rdinit=/sbin/init raid=noautodetect
bootm 0x00800000
```

Use transfer address `0x00800000` and image load/entry `0x02000000` exactly.
Do not substitute the default `0x8000`.

**Expected outcome:** Successful checksum verification, no reset, then
`Starting kernel ...` from this image.

**Stop immediately if you see:** `Image too large`, `Must RESET`, a new U-Boot
banner, a disk FIT image loading, `root=/dev/md0`, or `root=/dev/md1`. Any of
these indicates that the recovery boot failed.

The `raid=noautodetect` setting prevents automatic RAID assembly, but does not
prevent userspace from accessing disks. Booting into RAM therefore does not, on
its own, guarantee that attached storage receives no writes.

---

## 7. Verify RAM root before password or network changes

In **terminal U**, wait after `Starting kernel` until you see:
`Please press Enter to activate this console.`
Press Enter once. You should see an OpenWrt banner and `root@OpenWrt:~#` (or
temporarily `root@(none):~#`). It uses the root account with no password.
Stop if it asks for a password.

Before setting a password or changing network settings, run these read-only checks:

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
cat /proc/cmdline
awk '$2 == "/" { print "ROOT:", $1, $3, $4 }' /proc/mounts
tr '\000' '\n' < /proc/device-tree/compatible
cat /etc/openwrt_release
uname -r
cat /proc/mdstat
cat /proc/mounts
```

Check every item before proceeding:
- Command line contains `rdinit=/sbin/init` and `raid=noautodetect`, without any `root=` disk argument.
- Root filesystem type is `rootfs`, `ramfs`, or `tmpfs` (an `ext4` root is a disk root; `/dev/root` alone does not prove RAM storage).
- First compatible entry is `fujitsu,q703` (for Q703) or `qnap,ts221` (for TS-221).
- Release and kernel match step 2.
- **No internal filesystem is mounted.** If one is mounted, stop and identify the responsible service.

If any check fails, preserve the log and stop. You may be connected to an
existing disk installation rather than the RAM recovery system.

Inspect network interfaces:

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
ubus call network.interface.lan status
ip -f inet address show
```

Find the assigned DHCP address.

**Only if LAN received no DHCP address**, set the static address reserved in step 3:

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
uci set network.lan.proto=static
uci set network.lan.ipaddr=192.168.1.108
uci set network.lan.netmask=255.255.255.0
uci commit network
/etc/init.d/network restart
ip -f inet address show
```

Set a temporary root password, display the SSH host-key fingerprint, and verify
that the fan daemon (`qcontrol`) is running:

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
passwd
dropbearkey -y -f /etc/dropbear/dropbear_ed25519_host_key
pidof qcontrol
```

Verify that the fan is spinning. If the Ed25519 key does not exist, list
`/etc/dropbear` and check whichever key was generated.

In **terminal H**, connect over SSH using the observed NAS IP. Verify the host
key fingerprint matches terminal U before accepting the connection:

**Execution context:** [Host - Terminal H]
```sh
NAS_ADDRESS=192.168.1.108
SSH_OPTS=(-o "UserKnownHostsFile=$SESSION/known_hosts" -o StrictHostKeyChecking=ask)
ssh "${SSH_OPTS[@]}" root@"$NAS_ADDRESS"
```

Repeat the root and identity checks from the start of step 7 over SSH.
Type `exit` to return to the host shell in H. Confirm that you are back on the
build host:

**Execution context:** [Host - Terminal H]
```sh
test "$PWD" = "$REPO" && printf 'Back on build host in %s\n' "$REPO"
```

### The decision point

- **Unsigned RAM-recovery test:** Stop here and proceed to [Cleanup](#cleanup).
  RAM settings, password, and keys disappear on reboot. This test is intended
  to preserve the existing installation, provided the storage checks passed and
  no disk-writing commands were run.
- **Permanent installation:** Keep the NAS running in this verified RAM session
  and proceed to step 8. Steps 9–11 modify disks, persistent boot settings,
  and the NOR bootloader.

---

## 8. Sign and stage for permanent installation

**Continue only if you intend to install permanently.** Use only backed-up or
disposable disks. Step 9 replaces partition tables on `/dev/sda` and `/dev/sdb`.

Signing the exact qualified files for a local installation does not require a clean Git worktree, a new commit,
a push, a GitHub login, or a GitHub release.
If the build tree was dirty, `project_commit` records only its base commit; the
resulting bundle must not be presented as a reproducible public release. Public
publication requires rebuilding and qualifying from a clean committed tree as
described in [the release guide](update.md#publication-candidate).

Leave the NAS running in RAM recovery. In **terminal H**, verify release inputs:

**Execution context:** [Host - Terminal H]
```sh
SIGNING_INPUTS_OK=0
check_signing_inputs() {
    test "${RECOVERY_PREFLIGHT_OK:-0}" -eq 1 || return 1
    test "$PWD" = "$REPO" || {
        echo 'STOP: return from SSH to the repository root in H.' >&2
        return 1
    }
    EMBEDDED_REPOSITORY=$(
        tar -xOzf "dist/openwrt-$MODEL.tar.gz" ./rootfs.tar.gz |
            tar -xzOf - ./etc/firmware/repository
    ) || return
    test "$EMBEDDED_REPOSITORY" = "$GITHUB_REPOSITORY" || {
        echo 'STOP: qualified OpenWrt repository identity changed.' >&2
        return 1
    }
    cmp dist/release.pub keys/release.pub || return
    missing=0
    for file in dist/sources.json dist/release.pub dist/linux.tar.gz \
        dist/openwrt-q703.tar.gz dist/u-boot-q703.kwb dist/u-boot-q703.config \
        dist/openwrt-ts221.tar.gz dist/u-boot-ts221.kwb dist/u-boot-ts221.config; do
        if ! test -s "$file"; then
            printf 'STOP: local-signing input is missing: %s\n' "$file" >&2
            missing=1
        fi
    done
    test "$missing" -eq 0 || return
    python3 -c 'import json,re; assert re.fullmatch(r"[0-9a-f]{40}", json.load(open("dist/sources.json")).get("project_commit", "")), "dist/sources.json lacks project_commit"' || return
    printf 'Repository embedded in qualified image: %s\n' "$EMBEDDED_REPOSITORY"
}
if check_signing_inputs; then
    SIGNING_INPUTS_OK=1
    echo 'Local-signing inputs passed.'
else
    echo 'STOP: local-signing inputs failed.' >&2
fi
```

**Continue only when:** You see `Local-signing inputs passed.`

In H, enter the absolute path to your private release key. The key must have
mode `0400` or `0600` and reside outside the repository:

**Execution context:** [Host - Terminal H]
```sh
SIGNING_KEY_OK=0
select_release_key() {
    test "${SIGNING_INPUTS_OK:-0}" -eq 1 || return 1
    printf 'Absolute private release-key path: '
    read -r RELEASE_KEY || return
    case "$RELEASE_KEY" in
        /*) ;;
        *) echo 'STOP: use an absolute private-key path.' >&2; return 1 ;;
    esac
    case "$RELEASE_KEY" in
        "$REPO"/*) echo 'STOP: keep the private key outside the firmware repository.' >&2; return 1 ;;
    esac
    test -f "$RELEASE_KEY" || return
    case "$(stat -c %a "$RELEASE_KEY")" in
        400|600) ;;
        *) echo 'STOP: private key must have mode 0400 or 0600.' >&2; return 1 ;;
    esac
    CHECK_PUB=$(mktemp /tmp/q703-release-pub.XXXXXX) || return
    if pixi run openssl pkey -in "$RELEASE_KEY" -pubout -out "$CHECK_PUB" &&
       cmp "$CHECK_PUB" keys/release.pub; then
        rm -f "$CHECK_PUB" || return
    else
        rm -f "$CHECK_PUB"
        echo 'STOP: private key does not match keys/release.pub.' >&2
        return 1
    fi
    export RELEASE_KEY
}
if select_release_key; then
    SIGNING_KEY_OK=1
    echo 'Private release key matched.'
else
    echo 'STOP: release-key selection failed.' >&2
fi
```

Choose a new release tag (`vYYYY.MM.DD.N`) and sign locally:

**Execution context:** [Host - Terminal H]
```sh
SIGNED_RELEASE_OK=0
sign_local_release() {
    test "${SIGNING_KEY_OK:-0}" -eq 1 || return 1
    printf 'Local release tag (vYYYY.MM.DD.N): '
    read -r TAG || return
    export TAG
    pixi run release sign "$TAG" || return
    pixi run release verify "$TAG" || return
}
if sign_local_release; then
    SIGNED_RELEASE_OK=1
    echo 'Fresh local release signed and verified.'
else
    echo 'STOP: local signing or verification failed.' >&2
fi
```

Verify that all signed assets exist:

**Execution context:** [Host - Terminal H]
```sh
(
    set -eu
    test "${SIGNED_RELEASE_OK:-0}" -eq 1
    missing=0
    for file in \
        "dist/manifest-$MODEL.json" "dist/manifest-$MODEL.sig" \
        "dist/openwrt-$MODEL.tar.gz" "dist/u-boot-$MODEL.kwb" \
        dist/release.pub; do
        if ! test -s "$file"; then
            printf 'STOP: signed release asset is missing: %s\n' "$file" >&2
            missing=1
        fi
    done
    test "$missing" -eq 0
)
```

Verify the signature using OpenSSL directly:

**Execution context:** [Host - Terminal H]
```sh
pixi run openssl pkeyutl -verify -pubin -rawin -inkey keys/release.pub \
    -in "dist/manifest-$MODEL.json" -sigfile "dist/manifest-$MODEL.sig"
```

In **terminal U**, on the NAS RAM console, create `/tmp/install`:

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
test ! -e /tmp/install && mkdir -m 0700 /tmp/install
```

In **terminal H**, transfer the signed release bundle:

**Execution context:** [Host - Terminal H]
```sh
transfer_release() {
    test "${SIGNED_RELEASE_OK:-0}" -eq 1 || return 1
    cp "dist/manifest-$MODEL.json" "$SESSION/manifest.json" || return
    cp "dist/manifest-$MODEL.sig" "$SESSION/manifest.sig" || return
    tar -czf - -C "$REPO/dist" "openwrt-$MODEL.tar.gz" "u-boot-$MODEL.kwb" \
        -C "$SESSION" manifest.json manifest.sig |
        ssh "${SSH_OPTS[@]}" root@"$NAS_ADDRESS" 'tar -xzf - -C /tmp/install' || return
}
transfer_release || echo 'STOP: release transfer failed; do not continue on the NAS.' >&2
```

In **terminal U**, verify the staged files:

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
(
    set -eu
    . /usr/lib/firmware.sh
    verify_manifest /tmp/install
    verify_file "openwrt-$board.tar.gz"
    verify_file "u-boot-$board.kwb"
    echo 'Signed model, repository, sizes and hashes verified'
)
```

**Continue only when:** The final line confirms `Signed model, repository, sizes and hashes verified`.

Connect a USB drive formatted with ext4. Identify its block device by model,
serial, and size (not by drive letter alone):

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
cat /proc/partitions
for d in /sys/class/block/sd?; do
    echo "$d"
    cat "$d/device/model"
    cat "$d/device/serial" 2>/dev/null
done
```

Mount the verified partition (this example assumes `/dev/sdc1`):

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
mkdir -p /mnt/backup
mount -t ext4 /dev/sdc1 /mnt/backup
awk '$2 == "/mnt/backup" { print }' /proc/mounts
df -k /mnt/backup
```

**Continue only when:** `/mnt/backup` is mounted read-write and has ample free space.

---

## 9. Prepare system arrays

On the NAS console in U, check required tools and test `mkfs.ext4`:

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
(
    set -eu
    for tool in sfdisk mdadm blockdev mkfs.ext4 e2fsck mtd fw_printenv fw_setenv; do
        command -v "$tool"
    done
    dd if=/dev/zero of=/tmp/mkfs-probe bs=1M count=8
    mkfs.ext4 -q -F /tmp/mkfs-probe
    rm -f /tmp/mkfs-probe
)
```

Check sector size (must report `512`) and confirm neither disk is mounted or in use:

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
blockdev --getss /dev/sda
blockdev --getss /dev/sdb
cat /proc/mounts
cat /proc/swaps
cat /proc/mdstat
```

Before partitioning, identify both intended system drives by model, serial
number, capacity, and current layout. Device letters alone are insufficient:

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
for disk in /dev/sda /dev/sdb; do
    name=${disk##*/}
    printf '%s\n' "=== $disk ==="
    cat "/sys/class/block/$name/device/model"
    cat "/sys/class/block/$name/device/serial"
    blockdev --getsize64 "$disk"
    sfdisk --list "$disk"
done
```

Match this output to the physical drives before continuing. Existing arrays
require a reviewed assembly and layout check. Do not create arrays over existing
metadata or use this fresh-install procedure to repair degraded arrays.

### Readiness gate before destructive partitioning

Continue only after confirming all of the following:

- RAM-root verification passed.
- The exact qualified artifacts remain unchanged.
- Verified off-device backups exist.
- External backup storage is mounted, writable, and non-RAM.
- `/dev/sda` and `/dev/sdb` are the intended physical drives.
- No target disk, partition, RAID member, or swap device is in use.
- Stable power, UART, and the tested recovery path remain available.

> **Destructive operation: The next block overwrites partition tables on both
> `/dev/sda` and `/dev/sdb`. Run it only on two verified blank disks.**

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
(
    set -eu
    for disk in /dev/sda /dev/sdb; do
        sfdisk "$disk" <<'EOF'
label: gpt
start=524288, size=2097152, type=A19D880F-05FC-4D3B-A006-743F0F84911E
start=2621440, size=2097152, type=A19D880F-05FC-4D3B-A006-743F0F84911E
start=4718592, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4
EOF
    done
    mdadm --create /dev/md0 --metadata=0.90 --level=1 --raid-devices=2 /dev/sda1 /dev/sdb1
    mdadm --create /dev/md1 --metadata=0.90 --level=1 --raid-devices=2 /dev/sda2 /dev/sdb2
    mdadm --wait /dev/md0
    mdadm --wait /dev/md1
)
cat /proc/mdstat
mdadm --detail /dev/md0
mdadm --detail /dev/md1
```

**Continue only when:** Both RAID1 arrays show two active, healthy members,
metadata version `0.90`, and no resync or recovery is in progress.

Layout summary:

| System slot | Mirrored filesystem | Kernel location on each disk |
|---|---|---|
| A | `md0`: `sda1` + `sdb1` | LBA 2048 |
| B | `md1`: `sda2` + `sdb2` | LBA 133120 |

---

## 10. Install both system slots

> **Destructive step: This formats both system arrays and writes persistent
> boot settings.**

On the NAS in U, repeat step 7's RAM-root checks immediately before running this
command. Continue only if those checks still pass:

> A failure may follow completed storage writes. Keep the RAM session, UART logs,
> and backups available; do not reboot or retry blindly.

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
firmware-install init /tmp/install /mnt/backup --erase-system
```

Expected final message: `Both system slots installed. Qualify and flash this release U-Boot before rebooting.`

Record the exact NOR backup directory printed by the installer.
On the NAS, change into that directory and run `sha256sum -c SHA256SUMS`.
Both `u-boot.bin` and `RootFS2.bin` must pass.

Before proceeding, copy that backup to durable storage on the host and verify
it again using the block below. Keep these backups permanently.

In H, enter the exact NAS path printed by the installer and a new durable host
destination:

**Execution context:** [Host - Terminal H]
```sh
BACKUP_COPY_OK=0
copy_nor_backup_to_host() {
    printf 'Exact NAS backup directory: '
    read -r NAS_BACKUP || return
    printf 'New durable host backup directory: '
    read -r HOST_BACKUP || return
    case "$NAS_BACKUP" in /mnt/backup/firmware-*) ;; *) return 1 ;; esac
    case "$NAS_BACKUP" in *"'"*) return 1 ;; esac
    case "$HOST_BACKUP" in /*) ;; *) return 1 ;; esac
    test ! -e "$HOST_BACKUP" || return
    mkdir -m 0700 "$HOST_BACKUP" || return
    ssh "${SSH_OPTS[@]}" root@"$NAS_ADDRESS" \
        "tar -C '$NAS_BACKUP' -cf - u-boot.bin RootFS2.bin SHA256SUMS" |
        tar -xf - -C "$HOST_BACKUP" || return
    (cd "$HOST_BACKUP" && sha256sum -c SHA256SUMS) || return
}
if copy_nor_backup_to_host; then
    BACKUP_COPY_OK=1
    echo 'NOR backup copied to the host and verified.'
else
    echo 'STOP: backup copy/verification failed; preserve the destination for inspection.' >&2
fi
```

**Continue only when:** H prints `NOR backup copied to the host and verified.`

---

## 11. Activate and check permanent boot

<a id="nor-flashing"></a>

### NOR flashing

> **This step replaces the NAS's single bootloader in NOR flash.**
> Proceed only with stable power, verified off-device backups, and the exact
> U-Boot file that passed the RAM test. NOR flash has no A/B fallback.

In **terminal H**, check that the previous backup copy succeeded:

**Execution context:** [Host - Terminal H]
```sh
test "${BACKUP_COPY_OK:-0}" -eq 1 && echo 'Host NOR backup gate passed.'
```

If `Host NOR backup gate passed.` is not printed, stop.
Otherwise, return to **U's verified RAM console** and run the bootloader
replacement command:

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
firmware-install u-boot /tmp/install /mnt/backup --flash-nor
```

Expected output: `U-Boot verified. Reboot only after checking the boot environment.`
The flash command creates a fresh backup. Copy it to a **different** host
destination and verify it using step 10's procedure.

> If NOR flashing or readback verification fails, retain the RAM session and
> backups. Do not reboot, retry automatically, or guess a generic `mtd` command.

Confirm persistent boot settings in the `RootFS2` partition before rebooting:

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
(
    set -eu
    ENV_CONFIG=$(mktemp /tmp/q703-env-check.XXXXXX)
    trap 'rm -f "$ENV_CONFIG"' EXIT
    ENV_DEVICE=
    for d in /sys/class/mtd/mtd[0-9]*; do
        test "$(cat "$d/name")" = RootFS2 || continue
        test "$(cat "$d/offset")" = 13631488
        test "$(cat "$d/size")" = 3145728
        test -z "$ENV_DEVICE"
        ENV_DEVICE=/dev/${d##*/}
    done
    test -n "$ENV_DEVICE"
    printf '%s 0x180000 0x1000 0x10000\n%s 0x190000 0x1000 0x10000\n' \
        "$ENV_DEVICE" "$ENV_DEVICE" > "$ENV_CONFIG"
    fw_printenv -c "$ENV_CONFIG" firmware_layout active_slot rollback_slot \
        upgrade_available release_sequence bootcmd
)
```

Confirm that `firmware_layout=1`, `active_slot=A`, `rollback_slot=B`, and
`upgrade_available=1`.

Leave the backup directory on the NAS (`cd /`), flush writes, and unmount:

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
sync
umount /mnt/backup
awk '$2 == "/mnt/backup" { print }' /proc/mounts
```

**Continue only when:** `umount /mnt/backup` succeeds and the `awk` check prints
no mount entry. Otherwise keep the RAM session running and investigate.

Reboot into the newly installed OpenWrt system:

**Execution context:** [NAS/OpenWrt - Terminal U]
```sh
reboot
```

Watch terminal U during boot. Confirm:
- Slot A boots with `root=/dev/md0`, `firmware.layout=1`, and `firmware.slot=A`.
- RAID arrays are healthy, Ethernet has carrier, and fan control works.
- Drive fault LEDs are off and the status LED is green.

Rediscover the installed system's DHCP address from UART, then verify its newly
generated SSH fingerprint over UART before accepting it. Set the installed
system's password deliberately. The RAM password and static address do not
persist into the installed system.

Give the health service at least **90 seconds** to validate the installation.
Once confirmed, run the final verification checks on the installed NAS:

**Execution context:** [NAS/OpenWrt - Terminal U or SSH]
```sh
cat /proc/cmdline
cat /etc/firmware/installed
firmware-install status
cat /proc/mdstat
pidof qcontrol
ubus call network.interface.lan status
logread -e firmware
```

**Installation passes when:** `firmware-install status` succeeds and reports the
signed release's sequence, with the other checks above satisfied.

---

## Cleanup

Use this section when finishing successfully or abandoning a test. Cleanup
removes temporary host services and files; it does not undo an installation.

**If a storage write failed, keep the NAS running in RAM recovery and keep
its console and backups available for diagnosis.** Do not power it off or
reboot as part of routine cleanup.

1. If TFTP is still running in T, press `Ctrl-C`. Wait for `TFTP server stopped`
   and the T shell prompt. In H, run `sudo ss -lunp 'sport = :69'` and verify
   this session's Python listener is gone. Run `close_tftp_firewall`.
2. For a test-only RAM session, unmount external backup storage if mounted,
   then use `poweroff` when ready. Do not interrupt disk writes. RAM settings
   and `/tmp/install` vanish on reboot.
3. Exit kwboot by pressing `Ctrl-\`, releasing both keys, then lowercase `c`.
   In tmux, leave copy mode first. Identify the current pane from another
   terminal with the command below; substitute its actual target in send-keys:

   **Execution context:** [Host]
   ```sh
   tmux list-panes -a -F '#S:#I.#P #{pane_current_command} #{pane_tty}'
   # Replace SESSION:WINDOW.PANE with the actual kwboot pane:
   tmux send-keys -t SESSION:WINDOW.PANE -H 1c 63
   ```

   If kwboot still cannot exit, inspect the process on that exact pane's TTY.
   Terminate only the identified stuck kwboot process if necessary; do not
   destroy the pane or open another serial client. Run `stty sane` in its returned
   shell if terminal echo needs restoring.

4. In H, explicitly remove the firewall rule and session files, then disable
   the EXIT safeguard and leave the session shell:

   **Execution context:** [Host - Terminal H]
   ```sh
   if cleanup_session; then
       trap - EXIT
       exit
   else
       echo 'STOP: cleanup is incomplete; H remains open.' >&2
   fi
   ```

   `cleanup_session` verifies the firewall rule is gone when this run added
   it, then removes the named temporary files and empty directories. Treat any
   error as unfinished cleanup.

5. Restore any pre-existing service deliberately stopped, from your recorded
   prior state. This runbook creates no container or persistent TFTP service.
   Keep signed artifacts, source locks, keys, and verified backups.

### If the NAS cannot boot

If the NAS cannot boot, recover with a U-Boot/recovery pair that has already
passed testing on the hardware. Repeating an oversized or untested image does
not provide a working rescue path.

Restoring NOR and `RootFS2` alone does not restore QTS and your data.
A full rollback also needs partition-table, RAID, and data backups.

## Failure-signal index

| Signal | Meaning | Safe action |
|---|---|---|
| `Image too large` | Recovery did not start safely. | Stop; rebuild or restage the qualified pair. |
| `Unknown command` after `help mii` | The expected U-Boot capability is absent. | Stop; do not substitute another command. |
| `Retry count exceeded` | The TFTP transfer failed. | Retry once; then diagnose the link or firewall. |
| UDP port 69 already occupied | Another service owns the TFTP port. | Inspect the listed PID; do not terminate an unknown service. |
| `root=/dev/md0` or `root=/dev/md1` during RAM testing | The NAS booted a disk system. | Stop; do not continue toward installation. |
| Password prompt during initial RAM-console activation | The expected first-boot RAM state was not reached. | Stop and inspect the boot path. |
| `root@(none)` | Temporary RAM prompt before hostname setup. | Permitted; continue with the stated identity and root checks. |
| Firewall rollback failure | Temporary host firewall state may remain. | Keep H open and inspect the recorded rule before cleanup. |
