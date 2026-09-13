# Signing, releases and updates

This guide explains how to prepare a signed firmware release and how to update
a NAS that is already running the installed disk-root OpenWrt firmware. For
initial installation or an unsigned RAM recovery session, follow
[the installation runbook](install.md).

After completing this guide, you will be able to:

- Build and sign a consistent release.
- Install it through the automatic or manual update path.
- Verify trial boot, confirmation, or rollback.

## Where to start

Choose your starting point:

- **Update an installed NAS from GitHub:** Start at [step 5](#5-install-a-newer-openwrt-release).
- **Update an installed NAS without publishing:** Prepare a signed release in
  steps 1–3, then use [manual upgrade in step 6](#manual-upgrade).
- **Maintain or publish releases:** Follow steps 1–3; [step 4](#publish) adds
  optional GitHub publication.
- **Remove completed downloads:** Use [step 7](#7-remove-completed-download-staging)
  only after the update or rollback is confirmed.

## Working environment

- **Build host:** A Linux x86-64 computer, running commands in the repository
  root (the directory containing `pixi.toml`). Complete the README's
  [host setup](../README.md#1-prepare-the-build-computer) before building.
  Keep the same shell for steps 1–4: later commands use variables set in earlier steps.
- **NAS:** A root shell on the installed disk-root NAS, usually over SSH.

## How updates work

- **Signing boundary:** RAM-only recovery tests do not require signing. Permanent
  installation and automatic updates require a signed manifest verified against
  the embedded trust anchor.
- **Daily schedule:** The installed firmware checks its configured GitHub repository
  daily. Before installing, it verifies the Ed25519 digital signature, board model,
  manifest format, release sequence, file sizes, and SHA-256 hashes.
- **A/B dual-slot:** It writes OpenWrt to the inactive system slot and
  switches persistent boot settings last. The update takes effect at the next reboot.
  Recovery images running in RAM skip scheduled updates.
- **Bootloader separation:** Downloading a U-Boot file does not write to NOR
  flash. Bootloader activation is a separate, opt-in operation; see the
  [experimental automatic bootloader activation in step 5](#experimental-automatic-bootloader-activation).

## Glossary

- **Signing key:** An Ed25519 private key kept securely on the build host outside
  the repository. The matching public key is committed as `keys/release.pub` and
  embedded in the firmware.
- **Manifest:** A signed file (`manifest.json`) listing `project_commit`, release
  sequence, and the exact hashes and sizes of that model's OpenWrt and U-Boot files.
- **Release sequence:** A monotonically increasing number derived from signing time.
  The device accepts only releases with a sequence newer than its high-water mark.
- **Slot A / Slot B:** Independent RAID1 system volumes (`md0` and `md1`). Updates
  install to the inactive slot.
- **Trial boot:** The newly booted slot runs under observation. If health checks
  fail, the bootloader automatically rolls back to the previously working slot.
- **NOR flash:** The 16 MiB SPI bootloader chip. It has no A/B fallback.

---

<a id="key-setup"></a>

## 1. Optional: select and verify the signing key

Skip this step if you are installing an existing signed release. If you are
signing a release candidate in step 3, this step is required.

Keep the private key outside the repository with permissions `0400` or `0600`.
Never paste the key into a command line, log, issue, or chat.

**Execution context:** [Host]
```sh
RELEASE_KEY_OK=0
select_update_key() {
    printf 'Absolute private release-key path: '
    read -r RELEASE_KEY || return
    case "$RELEASE_KEY" in
        /*) ;;
        *) echo 'STOP: use an absolute private-key path.' >&2; return 1 ;;
    esac
    case "$RELEASE_KEY" in
        "$PWD"/*) echo 'STOP: keep the private key outside the firmware repository.' >&2; return 1 ;;
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
        return 1
    fi
    ( set -o pipefail
      pixi run openssl pkey -pubin -in keys/release.pub -outform DER |
          pixi run openssl dgst -sha256
    ) || return
    export RELEASE_KEY
}
if select_update_key; then
    RELEASE_KEY_OK=1
    echo 'Release key matched; fingerprint printed above.'
else
    echo 'STOP: release key verification failed; do not sign.' >&2
fi
```

**Continue only when:** The command prints `Release key matched; fingerprint printed above.`
Record that fingerprint through an independently trusted channel.

If the project has no release key yet, create an Ed25519 private key at an
external path with mode `0600`. Commit its public half as `keys/release.pub`,
back up the private key, and run the verification block above. Changing this key
later requires rebuilding and requalifying every image that embeds the public key.

---

<a id="publication-candidate"></a>

## 2. Build one internally consistent candidate

A candidate is the exact set of files you will test, sign, and install. Keep
that set unchanged between hardware testing, signing, and publication.

- **Public release:** Review and commit intended source changes first. The build
  block below requires a clean Git working tree (`test -z "$(git status --porcelain)"`)
  so that `project_commit` accurately identifies the built source.
- **Local-only installation:** Can use uncommitted changes, but `GITHUB_SHA`
  then identifies only the base commit. Do not publish such a build as reproducible.

For a local-only candidate, omit only the clean-tree check
`test -z "$(git status --porcelain)"` from the build block below. Retain the
repository identity, `GITHUB_SHA`, `resolve` source lock, and exact-artifact RAM
qualification. A dirty local candidate is never suitable for publication.

Set `GITHUB_REPOSITORY` to the real GitHub `owner/name` intended to serve updates:

**Execution context:** [Host]
```sh
REPOSITORY_OK=0
select_update_repository() {
    printf 'GitHub repository (owner/name): '
    read -r GITHUB_REPOSITORY || return
    case "$GITHUB_REPOSITORY" in
        OWNER/REPOSITORY|owner/name|'')
            echo 'STOP: enter the real repository identity, not the example.' >&2
            return 1
            ;;
    esac
    printf '%s\n' "$GITHUB_REPOSITORY" |
        grep -Eq '^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$' || return
    export GITHUB_REPOSITORY
}
if select_update_repository; then
    REPOSITORY_OK=1
else
    echo 'STOP: repository selection failed; do not build.' >&2
fi
```

Build the release candidate:

**Execution context:** [Host]
```sh
(
    set -eu
    test "${REPOSITORY_OK:-0}" -eq 1
    test -z "$(git status --porcelain)"
    GITHUB_SHA=$(git rev-parse HEAD)
    export GITHUB_SHA GITHUB_REPOSITORY
    pixi run resolve
    pixi run build
    pixi run test
    pixi run lint
)
```

`resolve` records upstream inputs in `work/sources.json`; `build` copies that
record to `dist/sources.json`. Keep both records. Do not resolve again or rebuild
between hardware qualification and signing.

**Hardware qualification requirement:** Before signing or publishing, follow
[installation steps 1–7](install.md#1-prepare-artifacts-and-terminals) for every
model you will update. Test the exact U-Boot and recovery files you intend to sign.

---

<a id="local-signing"></a>

## 3. Sign the qualified candidate locally

Choose an unused tag in `vYYYY.MM.DD.N` format. This step signs and verifies
local manifests; it makes no network requests and creates no GitHub release.

**Execution context:** [Host]
```sh
(
    set -eu
    printf 'Release tag (vYYYY.MM.DD.N): '
    read -r TAG
    test "${RELEASE_KEY_OK:-0}" -eq 1
    test "${REPOSITORY_OK:-0}" -eq 1
    : "${RELEASE_KEY:?repeat step 1 in this shell to select the private key}"
    test -f "$RELEASE_KEY"
    export GITHUB_REPOSITORY TAG RELEASE_KEY
    pixi run release sign "$TAG"
    pixi run release verify "$TAG"
)
```

**Expected outcome:** Both release commands succeed. The first signs one
manifest per model; the second verifies the complete signed set.

For a first installation, return to
[installation step 8](install.md#8-sign-and-stage-for-permanent-installation).
For an update without publication, proceed to [step 6](#manual-upgrade).

---

<a id="publish"></a>
<a id="draft-publication"></a>

## 4. Optional: upload a draft and publish it

Skip this section for local installation.

Verify that `project_commit` is already pushed to GitHub:

**Execution context:** [Host]
```sh
(
    set -eu
    test "${REPOSITORY_OK:-0}" -eq 1
    PROJECT_COMMIT=$(pixi run python -c \
        'import json; print(json.load(open("dist/sources.json"))["project_commit"])')
    pixi run gh api \
        "repos/$GITHUB_REPOSITORY/commits/$PROJECT_COMMIT" --silent
)
```

Create a draft release, download its uploaded assets, and compare them with
local `dist/` files:

**Execution context:** [Host]
```sh
(
    set -eu
    printf 'Signed release tag (vYYYY.MM.DD.N): '
    read -r TAG
    pixi run release draft "$TAG"
    pixi run gh release view "$TAG" --repo "$GITHUB_REPOSITORY"
    VERIFY_DIR=$(mktemp -d /tmp/q703-release-check.XXXXXX)
    trap 'rm -f "$VERIFY_DIR"/*; rmdir "$VERIFY_DIR"' EXIT
    pixi run gh release download "$TAG" --repo "$GITHUB_REPOSITORY" \
        --dir "$VERIFY_DIR"
    for file in "$VERIFY_DIR"/*; do
        cmp "$file" "dist/${file##*/}"
    done
    rm -f "$VERIFY_DIR"/*
    rmdir "$VERIFY_DIR"
    trap - EXIT
)
```

**Expected outcome:** Draft is created and all `cmp` checks succeed silently.

Test those exact downloaded bytes on hardware. A release containing both model
sets must be tested on both supported models, including recovery and rollback,
before publication. Once tests pass, publish the draft:

**Execution context:** [Host]
```sh
(
    set -eu
    test "${REPOSITORY_OK:-0}" -eq 1
    printf 'Qualified draft tag: '
    read -r TAG
    test -n "$TAG"
    pixi run gh release edit "$TAG" --draft=false --latest --repo "$GITHUB_REPOSITORY"
)
```

> **Warning:** Never replace assets on a published tag. Correct an issue by
> publishing a newer release tag.

---

## 5. Install a newer OpenWrt release

Start from the installed disk-root system, not RAM recovery. Verify running
status and RAID arrays:

**Execution context:** [NAS/OpenWrt]
```sh
cat /proc/cmdline
firmware-install status
cat /proc/mdstat
```

**Continue only if:** The command line includes `firmware.layout=1` and a
`firmware.slot` value, `firmware-install status` succeeds, and both `md0` and
`md1` are healthy and idle. A degraded, rebuilding, resyncing, or recovering
array is not ready for an update.

Trigger the update check and installation:

**Execution context:** [NAS/OpenWrt]
```sh
firmware-update
```

If a valid newer release exists, it downloads to `/root/updates/TAG` and installs
into the inactive slot. The running system remains online. Reboot when convenient
to begin the trial boot.

**After reboot:** Check system status and logs:

**Execution context:** [NAS/OpenWrt]
```sh
cat /proc/cmdline
cat /etc/firmware/installed
firmware-install status
cat /proc/mdstat
pidof qcontrol
ubus call network.interface.lan status
logread -e firmware
```

**Expected outcome:** The boot service checks active slot, rootfs, RAID arrays,
fan control, and network, then confirms the trial. If the trial fails, the
system reboots into the rollback slot.

An early hard hang may still require an external reset until watchdog recovery
is qualified.

Configuration is preserved via `sysupgrade -b`. Old packages or kernel modules
not present in the new image are not preserved. **Never use generic `sysupgrade`
to install firmware on this disk layout.**

<a id="experimental-automatic-bootloader-activation"></a>

### Experimental: automatic bootloader activation

Leave this feature disabled unless you have a verified NOR backup and have
rehearsed recovery over UART. Standard updates download U-Boot files but do not
flash them.

> **Risk:** NOR has no A/B fallback. A power failure during writing or an
> incompatible bootloader leaves the NAS unbootable, requiring UART `kwboot`
> recovery.

To enable automatic NOR flashing after a successful OpenWrt trial:

**Execution context:** [NAS/OpenWrt]
```sh
uci set firmware.nor.auto=1
uci set firmware.nor.backup_dir='/mnt/backup'   # separately mounted, non-RAM
uci commit firmware
```

With this option enabled, the daily updater compares the confirmed release's
U-Boot with NOR. If different, it creates a fresh backup on `/mnt/backup`,
writes the new bootloader, and verifies readback. A missing backup mount or
matching NOR skips flashing and leaves NOR unchanged. Other failures are logged
to `logread -e firmware`. If writing or readback fails, do not assume the
bootloader is usable; retain the RAM session and backup and follow the rehearsed
recovery procedure. Disable by setting `firmware.nor.auto` back to `0`.

---

<a id="manual-upgrade"></a>

## 6. Manual upgrade from a locally signed stage

Use this path to update without publishing to GitHub. It uses the signed set from
step 3 and enforces the same signature checks, inactive slot install, trial boot,
and sequence checks as the automatic updater.

Check installed system health using the pre-checks in [step 5](#5-install-a-newer-openwrt-release).
Note that U-Boot is verified but not flashed by this upgrade; for manual NOR
flashing, use the [installation guide's NOR flashing gate](install.md#nor-flashing).

Create a staging directory on the NAS:

**Execution context:** [NAS/OpenWrt]
```sh
(
    set -eu
    case "$(tr '\000' '\n' </proc/device-tree/compatible | head -n 1)" in
        fujitsu,q703) MODEL=q703 ;;
        qnap,ts221) MODEL=ts221 ;;
        *) echo 'Unsupported board' >&2; exit 1 ;;
    esac
    printf 'Local release tag (vYYYY.MM.DD.N): '
    read -r UPDATE_TAG
    printf '%s\n' "$UPDATE_TAG" | grep -Eq '^v[0-9][A-Za-z0-9._-]{0,63}$'
    test ! -e "/root/updates/$UPDATE_TAG"
    mkdir -p -m 700 "/root/updates/$UPDATE_TAG"
    printf 'Board %s; transfer the %s set into /root/updates/%s\n' \
        "$MODEL" "$MODEL" "$UPDATE_TAG"
)
```

On the build host, transfer the signed release bundle:

**Execution context:** [Host]
```sh
(
    set -eu
    UPDATE_TAG=v2026.09.12.3   # The tag signed in step 3.
    MODEL=q703                 # The board detected on the NAS.
    NAS_ADDRESS=192.168.1.109  # The installed system's address.
    tar -czf - -C dist "manifest-$MODEL.json" "manifest-$MODEL.sig" \
        "openwrt-$MODEL.tar.gz" "u-boot-$MODEL.kwb" |
        ssh root@"$NAS_ADDRESS" "cd /root/updates/$UPDATE_TAG &&
            tar -xzf - &&
            mv manifest-$MODEL.json manifest.json &&
            mv manifest-$MODEL.sig manifest.sig"
)
```

On the NAS, verify staged files and perform the upgrade:

**Execution context:** [NAS/OpenWrt]
```sh
(
    set -eu
    STAGE=/root/updates/v2026.09.12.3   # The stage created above.
    test -d "$STAGE"
    mkdir -p /root/updates
    exec 9>/root/updates/.lock
    flock -n 9
    . /usr/lib/firmware.sh
    verify_manifest "$STAGE"
    verify_file "openwrt-$board.tar.gz"
    verify_file "u-boot-$board.kwb"
    echo 'Signed model, repository, sizes and hashes verified'
    /usr/sbin/firmware-install upgrade "$STAGE"
)
```

**Expected outcome:** Verification passes and the upgrade completes.
Reboot when convenient and perform the post-update checks in
[step 5](#5-install-a-newer-openwrt-release). Remove staging only after the
update is confirmed.

---

<a id="7-remove-completed-download-staging"></a>

## 7. Remove completed download staging

Remove staging directories only after the new system or its rollback is confirmed,
and when no updater holds the lock.

List candidate staging directories on the NAS:

**Execution context:** [NAS/OpenWrt]
```sh
find /root/updates -mindepth 1 -maxdepth 1 -type d -print
```

Delete the completed release directory safely:

**Execution context:** [NAS/OpenWrt]
```sh
(
    set -eu
    printf 'Completed update tag: '
    read -r UPDATE_TAG
    printf '%s\n' "$UPDATE_TAG" | grep -Eq '^v[0-9][A-Za-z0-9._-]{0,63}$'
    case "$(tr '\000' '\n' </proc/device-tree/compatible | head -n 1)" in
        fujitsu,q703) MODEL=q703 ;;
        qnap,ts221) MODEL=ts221 ;;
        *) echo 'Unsupported board' >&2; exit 1 ;;
    esac
    STAGE=/root/updates/$UPDATE_TAG
    test -d "$STAGE"
    exec 9>/root/updates/.lock
    flock -n 9
    rm -f -- "$STAGE/manifest.json" "$STAGE/manifest.sig" \
        "$STAGE/openwrt-$MODEL.tar.gz" "$STAGE/u-boot-$MODEL.kwb"
    rmdir -- "$STAGE"
    flock -u 9
    exec 9>&-
)
```

`rmdir` removes the directory only if it is empty. If unexpected files remain,
inspect them rather than deleting recursively. Removing a staging directory does
not allow an older release to be replayed: the high-water mark sequence is
stored in the persistent boot environment.
