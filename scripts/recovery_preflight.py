#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validate and stage one U-Boot/OpenWrt RAM-recovery pair."""

from __future__ import annotations

import io
import hashlib
from pathlib import Path
import re
import struct
import sys
import tarfile
import zlib


def stop(message: str) -> None:
    raise SystemExit(f"STOP: {message}")


def require(condition: bool, message: str) -> None:
    if not condition:
        stop(message)


def archive_file(archive: tarfile.TarFile, name: str) -> bytes:
    member = next(
        (item for item in archive.getmembers() if item.name.lstrip("./") == name),
        None,
    )
    require(member is not None and member.isfile(), f"missing {name} in archive")
    source = archive.extractfile(member)
    require(source is not None, f"cannot read {name} from archive")
    return source.read()


def stage_uboot(source: Path, destination: Path) -> tuple[int, str]:
    data = source.read_bytes()
    require(0 < len(data) <= 512 * 1024, "U-Boot image exceeds 512 KiB NOR slot")
    # The staged copy is read-only after staging; drop it first so a rerun
    # with rebuilt artifacts replaces it instead of failing on 0444.
    destination.unlink(missing_ok=True)
    destination.write_bytes(data)
    destination.chmod(0o444)
    return len(data), hashlib.sha256(data).hexdigest()


def main() -> int:
    if len(sys.argv) != 3:
        stop("usage: recovery_preflight.py MODEL SESSION_DIR")
    model = sys.argv[1]
    require(model in ("q703", "ts221"), "MODEL must be q703 or ts221")

    repo = Path(__file__).resolve().parents[1]
    session = Path(sys.argv[2]).resolve()
    tftp_root = session / "tftp"
    require(session.is_dir() and tftp_root.is_dir(), "create the session/TFTP directories first")

    uboot = repo / "dist" / f"u-boot-{model}.kwb"
    config_path = repo / "dist" / f"u-boot-{model}.config"
    archive_path = repo / "dist" / f"openwrt-{model}.tar.gz"
    require(uboot.is_file(), f"missing {uboot}")
    require(config_path.is_file(), f"missing {config_path}")
    require(archive_path.is_file(), f"missing {archive_path}")

    config = config_path.read_text()
    match = re.search(r"^CONFIG_SYS_BOOTM_LEN=(0x[0-9a-fA-F]+|\d+)$", config, re.M)
    require(match is not None, "U-Boot config has no CONFIG_SYS_BOOTM_LEN")
    limit = int(match.group(1), 0)

    with tarfile.open(archive_path, "r:gz") as archive:
        image = archive_file(archive, "recovery.uImage")
        kernel_config = archive_file(archive, "kernel.config")
        rootfs = archive_file(archive, "rootfs.tar.gz")

    require(len(image) >= 64, "short recovery uImage")
    magic, header_crc, _epoch, size, load, entry, data_crc = struct.unpack(
        ">7I", image[:28]
    )
    require(magic == 0x27051956, "recovery file is not a legacy uImage")
    require(len(image) == size + 64, "truncated recovery uImage")
    header = image[:4] + b"\0" * 4 + image[8:64]
    require(zlib.crc32(header) == header_crc, "bad uImage header CRC")
    require(zlib.crc32(image[64:]) == data_crc, "bad uImage payload CRC")
    require(image[28:32] == bytes((5, 2, 2, 0)), "expected uncompressed Linux/ARM kernel")
    require(load == entry == 0x02000000, "unexpected uImage load/entry address")
    require(size <= limit, f"recovery payload {size} exceeds U-Boot limit {limit}; rebuild U-Boot")

    with tarfile.open(fileobj=io.BytesIO(rootfs), mode="r:gz") as archive:
        release = archive_file(archive, "etc/openwrt_release")

    staged = tftp_root / "recovery.uImage"
    staged.write_bytes(image)
    staged.chmod(0o644)
    staged_uboot = session / f"u-boot-{model}.kwb"
    uboot_size, uboot_sha256 = stage_uboot(uboot, staged_uboot)
    (session / "kernel.config").write_bytes(kernel_config)
    (session / "openwrt_release").write_bytes(release)

    print(release.decode(), end="" if release.endswith(b"\n") else "\n")
    print("Image:", image[32:64].rstrip(b"\0").decode())
    print(f"Bytes: {len(image)}; U-Boot filesize: {len(image):x}")
    print(f"U-Boot CRC32: {zlib.crc32(image):08x}; boot limit: {limit}")
    print(
        f"Staged U-Boot: {staged_uboot}; bytes: {uboot_size}; "
        f"SHA256: {uboot_sha256}"
    )
    print(
        "Expected compatible:",
        {"q703": "fujitsu,q703", "ts221": "qnap,ts221"}[model],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
