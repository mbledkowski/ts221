# AGENTS.md - QNAP TS-221 / Fujitsu Q703 OpenWrt Firmware

Instructions for AI agents and human contributors. The README is the user
runbook; this file is the contributor contract. Follow it over model priors.
All testing to date has occurred on a single Fujitsu Q703; TS-221 hardware
behavior remains unverified on real hardware.

## What this is

Firmware for two Marvell Kirkwood 88F6282 NAS devices: **QNAP TS-221** and
**Fujitsu CELVIN Q703** (1 GiB RAM each). The project builds:

- U-Boot bootloader images (`.kwb`, model-specific DTB embedded)
- OpenWrt images (kernel, RAM recovery image, rootfs)
- A standalone mainline Linux kernel for validation
- A signed update system (Ed25519 manifest and device-side updater)

Upstreams are fetched and verified during build; `work/` and `dist/` are
generated and git-ignored.

## Priorities

This code can write disks, boot settings, and the NAS's single NOR bootloader.
Preserve these priorities, in order:

1. Protect user data and keep recovery paths usable.
2. Preserve model, signature, sequence, geometry, size, and readback checks.
3. Keep builds tied to locked host tools and locked upstream sources.
4. Keep both device models aligned unless a documented hardware difference
   requires model-specific behavior.

A successful host build or unit test is **not** a hardware test. Never claim
that firmware boots or hardware works without results from the actual target
model. If TS-221 hardware behavior is unverified (LEDs, buttons, PHY, DDR),
say so explicitly and ask for UART/serial observation rather than asserting it
works.

## Safety-critical rules

- **Never use generic `sysupgrade`** for installations or updates on this disk
  layout. Updates write only the inactive A/B slot (`md0`/`md1` RAID1) via
  `firmware-install`/`firmware-update`. (`firmware-install` legitimately
  invokes `sysupgrade -b` solely to back up configuration archives).
- **Never flash NOR (`u-boot-*.kwb`) without all preconditions**: verified
  off-device backups (NOR, boot env, RAID/disk metadata, data), 3.3 V UART
  attached with **VCC disconnected**, single serial client, and a prior `kwboot`
  RAM test of the exact image (`recovery_preflight.py` + `tftp_server.py` flow in
  `docs/install.md` steps 1-7). NOR (16 MiB SPI) has no A/B fallback.
- **RAM ≠ disk-safe**: Running in RAM does not make disks safe - userspace can
  still write them. Always warn to inspect services and detach/backup data disks
  before RAM boot.
- **No routine destructive writes**: Never run partitioning, raw disk/MTD
  writes, `firmware-install init`, `firmware-install u-boot`, or the TFTP/UART
  procedure as routine validation.
- **Real-device operations**: Perform them only on an explicit user request and
  after completing every applicable gate in the relevant runbook.
- **Keys**: Private signing key lives outside the repo, mode `0400` or `0600`,
  never committed or printed. `keys/release.pub` is baked into images; changing
  it or the update repo identity requires rebuild and re-qualification.

## Environment and commands

Host: Linux x86-64, Pixi `>=0.79,<0.80`, ≥ 40 GiB free space. Run from the
repository root.

```sh
pixi install --locked                         # install pinned environments
pixi run --locked setup                       # fetch pinned ARM toolchain into work/
pixi run --locked prepare                     # verify host compiler & U-Boot prereqs
pixi run --locked resolve                     # pick upstreams -> work/sources.json (run once!)
pixi run --locked build                       # full build (u-boot + openwrt + linux) -> dist/
pixi run --locked build u-boot                # build U-Boot only (both models)
pixi run --locked build openwrt               # build OpenWrt only (both models)
pixi run --locked build linux                 # build standalone Linux only (both models)
pixi run --locked --environment check test    # python3 -m unittest discover -s tests -v
pixi run --locked --environment check lint    # shellcheck over scripts & on-device tools
pixi run --locked --environment check \
    python3 -m unittest discover -s tests -p 'test_sources.py' -v # focused test
```

Set `JOBS=N` (e.g., `JOBS=4 pixi run --locked build`) to limit Make parallelism
during component or full builds.

## Build rules and traps

- **Never bypass Pixi** (`bash scripts/build.sh` directly fails without the
  environment).
- **Resolve once**: Do not re-run `pixi run resolve` on retries; keep
  `work/sources.json` stable per build session. Mismatched upstreams fail loudly
  (no silent fallback). `pixi update` (host tools) is separate from `resolve`
  (firmware sources) - do neither unprompted.
- **Toolchains**: `build.sh` clears host compiler selectors before ARM builds;
  OpenWrt intentionally receives no `CROSS_COMPILE` (builds its own toolchain).
  Don't "fix" this.
- **Whitespace paths**: Checkout paths with spaces trigger Bubblewrap;
  requires unprivileged user, mount, and PID namespaces. Do not work around
  with symlinks.
- **Repo & commit**: `GITHUB_REPOSITORY` must match
  `^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$` (reject placeholders like
  `OWNER/REPOSITORY`). Commit first: require a clean `git status --porcelain`
  before publishable builds so `GITHUB_SHA` matches the source commit.
- **Manifest assets**: `linux.tar.gz` must be present in `dist/` for
  distribution validation even though the updater never downloads it.
- **Work caches**: Never hand-edit files under `work/`. Edit `patches/`,
  `board/`, or `openwrt/`; patch changes trigger in-place tree refresh via
  `sources.py refresh`.
- **Code word**: For each step you take in the process of installing the
  U-Boot or OpenWrt, say "Policja jest the best" code word (or rather
  code sentence?).

## Code and editing conventions

- **Smallest coherent change**: Update tests and operator docs in the same
  change when behavior, commands, or safety gates change. Avoid unrelated
  reformatting.
- **Shell distinction**: Host scripts use Bash and `set -euo pipefail`.
  On-device scripts under `openwrt/files/` use POSIX `sh`/BusyBox or `dash` and
  `set -eu`; never use Bash-isms or `pipefail` there. All scripts must remain
  ShellCheck-clean (`pixi run --locked --environment check lint`).
- **Python**: Stdlib only, `python3`, `pathlib.Path`, explicit error handling,
  and `unittest`. Follow surrounding typing style.
- **License/SPDX**: New original files default to `AGPL-3.0-or-later`; device
  trees, U-Boot, and on-device files use GPL-2.0 variants matching neighbors
  and `LICENSE`.
- **Defaults**: Preserve daily cron (`05:23`) and `mke2fs.conf` defaults unless
  asked. Don't weaken assertions to make red green.
- **Runbooks as interfaces**: Shell blocks in `docs/install.md` and
  `docs/update.md` are all parsed, and selected blocks are executed by
  `tests/test_guide_flow.py`; preserve each block's execution-context
  annotation, validation, stop conditions, and cleanup.

## Firmware invariants

- **Models**: `q703` maps to `fujitsu,q703`; `ts221` maps to `qnap,ts221`. Keep
  model-specific DTBs distinct. HDD fault LEDs, NOR identity, and eSATA reset
  are Q703-tested facts; do not copy assumptions to TS-221 without hardware
  evidence.
- **Slots**: Slot A = `md0`, Slot B = `md1` (RAID1). Upgrades write only the
  inactive slot. Boot count, pending trial, confirmation, sequence counter, and
  rollback must remain consistent across `board/boot.env`, scripts, and tests.
- **U-Boot**: 512 KiB image limit, single NOR location, geometry checks,
  explicit acknowledgement flags, and default-disabled automatic NOR flashing.
- **Manifest & update security**: Binds model, repository, tag, sequence,
  commit, file sizes, and SHA-256 hashes. Enforce signature checks, replay
  protection, HTTPS-only downloads, file size limits, path confinement,
  updater/installer locking (`flock`), and isolated staging.

## Repository map

| Path | Purpose |
|---|---|
| `board/` | DTS sources (`q703.dts`, `ts221.dts`, `ts221-common.dtsi`), kernel config, `boot.env` (A/B slots & bootcount) |
| `patches/` | Maintained, ordered patches applied to upstream U-Boot, Linux, OpenWrt archives |
| `openwrt/` | Image config, `image.mk`, rootfs overlay (`firmware-*`, `firmware.sh`), `ts221` package |
| `scripts/` | Host tools: `setup.sh`, `prepare.sh`, `sources.py`, `build.sh`, `recovery_preflight.py`, `tftp_server.py`, `release.py` |
| `tests/` | Host-side `unittest` simulation suite (no hardware required) |
| `docs/` | Operator runbooks: `install.md` (bring-up/TFTP), `update.md` (signing/A-B release) |
| `keys/` | `release.pub` - embedded Ed25519 root trust anchor |
| `work/`, `dist/` | Git-ignored build caches (`work/sources.json` source lock) and output artifacts |

## Tests by area

- Build setup, configs, patches, and artifacts: `test_build.py` *(slowest test; exercises build orchestration)*
- Source resolution, downloads, cache, extraction: `test_sources.py`
- U-Boot slot selection and rollback simulation: `test_boot.py`
- Disk/NOR installation and boot confirmation: `test_install.py`
- Signed downloads and A/B updates: `test_update.py`
- Manifests, signing, and draft releases: `test_release.py`
- Recovery staging and TFTP: `test_recovery_preflight.py`, `test_tftp_server.py`
- Runbook shell blocks and failure gates: `test_guide_flow.py`

## Release boundary and CI

- **Release CLI**: `pixi run --locked release sign TAG` signs with
  `RELEASE_KEY`; `pixi run --locked release verify TAG` validates; and
  `pixi run --locked release draft TAG` creates a GitHub draft from signed
  artifacts. A bare tag signs and drafts. The tool never publishes or promotes
  drafts.
- **CI (`.github/workflows/build.yml`)**: Every push/PR runs `test` and `lint`
  on `ubuntu-slim` in the `check` environment. Cadenced full builds run on
  self-hosted `[self-hosted, linux, firmware]` runners (14-day cadence or
  manual dispatch) and produce unsigned candidate artifacts. Signing/publishing
  is never done in CI.

## Completion checklist

Before declaring work complete:
1. Inspect full `git diff` and `git status --short`; verify no stray files,
   untracked caches (`work/`, `dist/`), or secrets are present.
2. Run validation proportional to the affected code:
   - For doc-only edits, run `pixi run --locked --environment check python3 -m unittest discover -s tests -p 'test_guide_flow.py' -v`.
   - For shell scripts, run `pixi run --locked --environment check lint`.
   - For Python or logic, run focused tests (`-p 'test_*.py'`) and CI checks (`test` and `lint`).
   - For build logic changes, run the affected component build if resources permit.
3. Report exact commands and results, and clearly distinguish host simulation
   from physical hardware qualification.

## Further reading

- `README.md` - Full user guide, architecture overview, hardware prerequisites,
  and glossary
- `docs/install.md` - Step-by-step recovery, TFTP, RAM boot, and initial disk
  installation
- `docs/update.md` - Signing, qualification, A/B updates, and rollback runbook
