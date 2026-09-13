#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Serve recovery images to U-Boot over TFTP."""

from __future__ import annotations

import os
import pwd
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

OPCODE_RRQ = 1
OPCODE_DATA = 3
OPCODE_ACK = 4
OPCODE_ERROR = 5
BLOCK_SIZE = 512


class ActiveTransfer(NamedTuple):
    cancel: threading.Event
    resend: threading.Event
    sock: socket.socket
    thread: threading.Thread
    peer: tuple[str, int]
    path: Path


def parse_rrq(payload: bytes) -> str | None:
    """Return the requested filename and ignore optional RRQ fields."""
    if len(payload) < 2 or payload[:2] != b"\x00\x01":
        return None
    fields = payload[2:].split(b"\x00")
    if not fields or not fields[0]:
        return None
    return fields[0].decode("ascii", "replace")


def resolve_request(root: Path, name: str) -> Path | None:
    """Resolve a request to a regular file directly below the TFTP root."""
    if os.path.basename(name) != name:
        return None
    try:
        path = (root / name).resolve(strict=True)
    except FileNotFoundError:
        return None
    return path if path.parent == root and path.is_file() else None


def send_error(sock: socket.socket, peer: tuple, message: str, code: int = 1) -> None:
    packet = struct.pack("!HH", OPCODE_ERROR, code)
    packet += message.encode("ascii", "replace") + b"\x00"
    sock.sendto(packet, peer)


def serve_file(
    sock: socket.socket,
    peer: tuple,
    path: Path,
    cancel: threading.Event | None = None,
    resend: threading.Event | None = None,
) -> None:
    data = path.read_bytes()
    pace = 0.0 if peer[0].startswith("127.") else float(
        os.environ.get("TFTP_PACE_S", "0.015")
    )
    ack_timeout = float(os.environ.get("TFTP_ACK_S", "5.0"))
    block = 1
    offset = 0

    while True:
        if cancel is not None and cancel.is_set():
            return
        chunk = data[offset : offset + BLOCK_SIZE]
        packet = struct.pack("!HH", OPCODE_DATA, block) + chunk
        acknowledged = False
        timed_out = 0
        while timed_out < 12:
            if cancel is not None and cancel.is_set():
                return
            try:
                sock.sendto(packet, peer)
            except OSError:
                if cancel is not None and cancel.is_set():
                    return
                raise
            deadline = time.monotonic() + ack_timeout
            resend_requested = False
            while True:
                if cancel is not None and cancel.is_set():
                    return
                if resend is not None and resend.is_set():
                    resend.clear()
                    resend_requested = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                wait = min(remaining, 0.1) if cancel is not None else remaining
                try:
                    sock.settimeout(wait)
                except OSError:
                    if cancel is not None and cancel.is_set():
                        return
                    raise
                try:
                    response, response_peer = sock.recvfrom(1024)
                except socket.timeout:
                    if cancel is not None and time.monotonic() < deadline:
                        continue
                    break
                except OSError:
                    if cancel is not None and cancel.is_set():
                        return
                    raise
                if response_peer != peer:
                    try:
                        send_error(
                            sock,
                            response_peer,
                            "unknown transfer ID",
                            code=5,
                        )
                    except OSError:
                        if cancel is not None and cancel.is_set():
                            return
                        raise
                    continue
                if len(response) < 4:
                    continue
                opcode, ack_block = struct.unpack("!HH", response[:4])
                if opcode == OPCODE_ACK and ack_block == block:
                    acknowledged = True
                    break
                if opcode == OPCODE_RRQ:
                    resend_requested = True
                    break
            if acknowledged:
                break
            if resend_requested:
                continue
            timed_out += 1
        else:
            print(f"timeout block={block} peer={peer}", flush=True)
            return

        offset += len(chunk)
        if len(chunk) < BLOCK_SIZE:
            print(f"sent {path.name} {len(data)} bytes to {peer[0]}", flush=True)
            return
        block = (block + 1) & 0xFFFF
        if pace > 0:
            time.sleep(pace)


def serve_request(listener: socket.socket, root: Path) -> None:
    payload, peer = listener.recvfrom(2048)
    name = parse_rrq(payload)
    if name is None:
        return
    path = resolve_request(root, name)
    if path is None:
        print(f"missing {os.path.basename(name)} from {peer[0]}", flush=True)
        send_error(listener, peer, "file not found")
        return

    print(f"request {path.name} from {peer[0]}", flush=True)
    transfer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    transfer.bind(("0.0.0.0", 0))
    try:
        serve_file(transfer, peer, path)
    finally:
        transfer.close()


def stop_transfer(transfer: ActiveTransfer) -> None:
    transfer.cancel.set()
    transfer.sock.close()
    if transfer.thread.ident is not None:
        transfer.thread.join(timeout=1)


def serve_forever(
    listener: socket.socket,
    root: Path,
    stop: threading.Event | None = None,
) -> None:
    """Serve requests while allowing a new RRQ to replace a stalled transfer."""
    active: dict[str, ActiveTransfer] = {}
    listener.settimeout(0.1 if stop is not None else None)
    try:
        while stop is None or not stop.is_set():
            try:
                payload, peer = listener.recvfrom(2048)
            except socket.timeout:
                continue

            name = parse_rrq(payload)
            if name is None:
                continue
            path = resolve_request(root, name)
            if path is None:
                print(f"missing {os.path.basename(name)} from {peer[0]}", flush=True)
                send_error(listener, peer, "file not found")
                continue

            previous = active.get(peer[0])
            if previous is not None:
                if (
                    previous.peer == peer
                    and previous.path == path
                    and previous.thread.is_alive()
                ):
                    previous.resend.set()
                    continue
                active.pop(peer[0])
                stop_transfer(previous)

            print(f"request {path.name} from {peer[0]}", flush=True)
            transfer_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            transfer_socket.bind(("0.0.0.0", 0))
            cancel = threading.Event()
            resend = threading.Event()

            def run_transfer(
                sock: socket.socket = transfer_socket,
                client: tuple = peer,
                requested: Path = path,
                cancelled: threading.Event = cancel,
                retry: threading.Event = resend,
            ) -> None:
                try:
                    serve_file(sock, client, requested, cancelled, retry)
                finally:
                    sock.close()

            thread = threading.Thread(
                target=run_transfer,
                name=f"tftp:{peer[0]}:{peer[1]}",
            )
            active[peer[0]] = ActiveTransfer(
                cancel,
                resend,
                transfer_socket,
                thread,
                peer,
                path,
            )
            thread.start()
    finally:
        for transfer in active.values():
            stop_transfer(transfer)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: sudo {Path(sys.argv[0]).name} TFTP_ROOT")

    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        raise SystemExit(f"TFTP root is not a directory: {root}")

    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        listener.bind(("0.0.0.0", 69))
    except PermissionError as error:
        raise SystemExit("binding UDP port 69 requires root; run with sudo") from error
    except OSError as error:
        raise SystemExit(f"cannot bind UDP port 69: {error}") from error

    display_root = root
    if os.geteuid() == 0:
        account = pwd.getpwnam("nobody")
        os.chroot(root)
        os.chdir("/")
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
        root = Path("/")

    print(
        f"TFTP root={display_root} listening on 0.0.0.0:69 uid={os.geteuid()}",
        flush=True,
    )
    try:
        serve_forever(listener, root)
    except KeyboardInterrupt:
        print("TFTP server stopped", flush=True)
    finally:
        listener.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
