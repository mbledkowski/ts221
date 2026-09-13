# SPDX-License-Identifier: AGPL-3.0-or-later
import errno
import importlib.util
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/tftp_server.py"
SPEC = importlib.util.spec_from_file_location("tftp_server", SCRIPT)
assert SPEC and SPEC.loader
TFTP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TFTP)


class ScriptedSocket:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def sendto(self, packet, peer):
        self.sent.append((packet, peer))

    def settimeout(self, seconds):
        pass

    def recvfrom(self, size):
        if not self.responses:
            raise socket.timeout
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class TftpServerTests(unittest.TestCase):
    def test_parses_rrq_and_ignores_options(self):
        request = b"\x00\x01recovery.uImage\x00octet\x00blksize\x001468\x00"
        self.assertEqual(TFTP.parse_rrq(request), "recovery.uImage")
        self.assertIsNone(TFTP.parse_rrq(b"\x00\x04\x00\x01"))

    def test_resolves_only_regular_files_in_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            image = root / "recovery.uImage"
            image.write_bytes(b"recovery")
            outside = root.parent / f"{root.name}-outside"
            outside.write_bytes(b"outside")
            self.addCleanup(outside.unlink)

            self.assertEqual(TFTP.resolve_request(root, image.name), image)
            self.assertIsNone(TFTP.resolve_request(root, f"../{image.name}"))
            (root / "outside-link").symlink_to(outside)
            self.assertIsNone(TFTP.resolve_request(root, "outside-link"))
            self.assertIsNone(TFTP.resolve_request(root, "missing"))

    def test_serves_complete_file_over_udp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            expected = bytes(range(256)) * 4
            (root / "recovery.uImage").write_bytes(expected)
            listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            listener.bind(("127.0.0.1", 0))
            errors = []

            def serve():
                try:
                    TFTP.serve_request(listener, root)
                except Exception as error:  # Propagate thread failures below.
                    errors.append(error)

            thread = threading.Thread(target=serve)
            thread.start()
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.settimeout(2)
            client.sendto(
                b"\x00\x01recovery.uImage\x00octet\x00",
                listener.getsockname(),
            )

            actual = bytearray()
            block = 1
            while True:
                packet, peer = client.recvfrom(516)
                opcode, received_block = struct.unpack("!HH", packet[:4])
                self.assertEqual((opcode, received_block), (TFTP.OPCODE_DATA, block))
                chunk = packet[4:]
                actual.extend(chunk)
                client.sendto(struct.pack("!HH", TFTP.OPCODE_ACK, block), peer)
                if len(chunk) < TFTP.BLOCK_SIZE:
                    break
                block = (block + 1) & 0xFFFF

            thread.join(timeout=2)
            client.close()
            listener.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(actual, expected)

    def test_new_request_replaces_stalled_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "recovery.uImage").write_bytes(b"recovery")
            listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            listener.bind(("127.0.0.1", 0))
            self.addCleanup(listener.close)
            stop = threading.Event()
            errors = []

            def serve():
                try:
                    TFTP.serve_forever(listener, root, stop)
                except Exception as error:  # Propagate thread failures below.
                    errors.append(error)

            thread = threading.Thread(target=serve)
            thread.start()
            self.addCleanup(stop.set)
            self.addCleanup(thread.join, 2)
            first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.addCleanup(first.close)
            first.settimeout(1)
            second = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.addCleanup(second.close)
            second.settimeout(1)
            request = b"\x00\x01recovery.uImage\x00octet\x00"
            first.sendto(request, listener.getsockname())
            first.recvfrom(516)

            second.sendto(request, listener.getsockname())
            packet, peer = second.recvfrom(516)
            self.assertEqual(
                struct.unpack("!HH", packet[:4]),
                (TFTP.OPCODE_DATA, 1),
            )
            second.sendto(struct.pack("!HH", TFTP.OPCODE_ACK, 1), peer)

            stop.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertFalse(
                [
                    worker
                    for worker in threading.enumerate()
                    if worker.name.startswith("tftp:")
                ]
            )

    def test_duplicate_rrq_keeps_the_original_transfer_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "recovery.uImage").write_bytes(b"recovery")
            listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            listener.bind(("127.0.0.1", 0))
            stop = threading.Event()
            errors = []

            def serve():
                try:
                    TFTP.serve_forever(listener, root, stop)
                except Exception as error:  # Propagate thread failures below.
                    errors.append(error)

            thread = threading.Thread(target=serve)
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.bind(("127.0.0.1", 0))
            client.settimeout(1)

            def stop_server():
                stop.set()
                thread.join(timeout=2)
                listener.close()
                client.close()

            self.addCleanup(stop_server)
            request = b"\x00\x01recovery.uImage\x00octet\x00"
            with mock.patch.dict("os.environ", {"TFTP_ACK_S": "2"}):
                thread.start()
                client.sendto(request, listener.getsockname())
                first, transfer_peer = client.recvfrom(516)

                for _ in range(16):
                    client.sendto(request, listener.getsockname())
                    retried, retry_peer = client.recvfrom(516)
                    self.assertEqual(retried, first)
                    self.assertEqual(retry_peer, transfer_peer)
                client.sendto(
                    struct.pack("!HH", TFTP.OPCODE_ACK, 1),
                    transfer_peer,
                )

            stop.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_repeated_abandoned_requests_then_complete_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            expected = bytes(range(256)) * 4 + b"last"
            (root / "recovery.uImage").write_bytes(expected)
            listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            listener.bind(("127.0.0.1", 0))
            stop = threading.Event()
            errors = []

            def serve():
                try:
                    TFTP.serve_forever(listener, root, stop)
                except Exception as error:  # Propagate thread failures below.
                    errors.append(error)

            thread = threading.Thread(target=serve)
            thread.start()

            def stop_server():
                stop.set()
                thread.join(timeout=2)
                listener.close()

            self.addCleanup(stop_server)
            request = b"\x00\x01recovery.uImage\x00octet\x00"

            with mock.patch.dict("os.environ", {"TFTP_ACK_S": "0.05"}):
                for _ in range(16):
                    abandoned = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    abandoned.settimeout(1)
                    try:
                        abandoned.sendto(request, listener.getsockname())
                        packet, _ = abandoned.recvfrom(516)
                        self.assertEqual(
                            struct.unpack("!HH", packet[:4]),
                            (TFTP.OPCODE_DATA, 1),
                        )
                    finally:
                        abandoned.close()

                client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                client.settimeout(1)
                try:
                    client.sendto(request, listener.getsockname())
                    actual = bytearray()
                    block = 1
                    while True:
                        packet, peer = client.recvfrom(516)
                        opcode, received_block = struct.unpack("!HH", packet[:4])
                        self.assertEqual(
                            (opcode, received_block),
                            (TFTP.OPCODE_DATA, block),
                        )
                        chunk = packet[4:]
                        actual.extend(chunk)
                        client.sendto(
                            struct.pack("!HH", TFTP.OPCODE_ACK, block),
                            peer,
                        )
                        if len(chunk) < TFTP.BLOCK_SIZE:
                            break
                        block = (block + 1) & 0xFFFF
                finally:
                    client.close()

            stop.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(actual, expected)
            self.assertFalse(
                [
                    worker
                    for worker in threading.enumerate()
                    if worker.name.startswith("tftp:")
                ]
            )

    def test_shutdown_cancels_transfer_waiting_for_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "recovery.uImage").write_bytes(b"recovery")
            listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            listener.bind(("127.0.0.1", 0))
            stop = threading.Event()
            errors = []

            def serve():
                try:
                    TFTP.serve_forever(listener, root, stop)
                except Exception as error:  # Propagate thread failures below.
                    errors.append(error)

            thread = threading.Thread(target=serve)
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.settimeout(1)

            def stop_server():
                stop.set()
                thread.join(timeout=2)
                listener.close()
                client.close()

            self.addCleanup(stop_server)
            request = b"\x00\x01recovery.uImage\x00octet\x00"
            with mock.patch.dict("os.environ", {"TFTP_ACK_S": "2"}):
                thread.start()
                client.sendto(request, listener.getsockname())
                packet, _ = client.recvfrom(516)
                self.assertEqual(
                    struct.unpack("!HH", packet[:4]),
                    (TFTP.OPCODE_DATA, 1),
                )

                stop.set()
                thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertFalse(
                [
                    worker
                    for worker in threading.enumerate()
                    if worker.name.startswith("tftp:")
                ]
            )

    def test_stale_ack_does_not_retransmit_or_consume_retry(self):
        peer = ("127.0.0.1", 12345)
        ack = lambda block: (struct.pack("!HH", TFTP.OPCODE_ACK, block), peer)
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "recovery.uImage"
            image.write_bytes(b"x" * 513)
            sock = ScriptedSocket([ack(1), ack(1), ack(2)])

            TFTP.serve_file(sock, peer, image)

        sent_blocks = [struct.unpack("!HH", packet[:4])[1] for packet, _ in sock.sent]
        self.assertEqual(sent_blocks, [1, 2])

    def test_duplicate_ack_flood_does_not_exhaust_retries(self):
        peer = ("127.0.0.1", 12345)
        ack = lambda block: (struct.pack("!HH", TFTP.OPCODE_ACK, block), peer)
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "recovery.uImage"
            image.write_bytes(b"x")
            sock = ScriptedSocket([ack(0)] * 12 + [ack(1)])

            TFTP.serve_file(sock, peer, image)

        data_packets = [
            packet for packet, _ in sock.sent if packet[:2] == b"\x00\x03"
        ]
        self.assertEqual(len(data_packets), 1)

    def test_lost_ack_retransmits_same_data_packet(self):
        peer = ("127.0.0.1", 12345)
        ack = (struct.pack("!HH", TFTP.OPCODE_ACK, 1), peer)
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "recovery.uImage"
            image.write_bytes(b"x")
            sock = ScriptedSocket([socket.timeout(), ack])

            TFTP.serve_file(sock, peer, image)

        data_packets = [
            packet for packet, _ in sock.sent if packet[:2] == b"\x00\x03"
        ]
        self.assertEqual(len(data_packets), 2)
        self.assertEqual(data_packets[0], data_packets[1])

    def test_cancel_during_timeout_setup_is_clean(self):
        peer = ("127.0.0.1", 12345)
        cancel = threading.Event()

        class ClosingSocket(ScriptedSocket):
            def settimeout(self, seconds):
                cancel.set()
                raise OSError(errno.EBADF, "Bad file descriptor")

        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "recovery.uImage"
            image.write_bytes(b"recovery")
            sock = ClosingSocket([])

            TFTP.serve_file(sock, peer, image, cancel)

    def test_lost_ack_retransmits_over_udp_and_then_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "recovery.uImage"
            image.write_bytes(b"recovery")
            transfer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            transfer.bind(("127.0.0.1", 0))
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.bind(("127.0.0.1", 0))
            client.settimeout(1)
            cancel = threading.Event()
            errors = []

            def serve():
                try:
                    TFTP.serve_file(transfer, client.getsockname(), image, cancel)
                except Exception as error:  # Propagate thread failures below.
                    errors.append(error)

            thread = threading.Thread(target=serve)

            def stop_transfer():
                cancel.set()
                transfer.close()
                client.close()
                thread.join(timeout=2)

            self.addCleanup(stop_transfer)
            with mock.patch.dict("os.environ", {"TFTP_ACK_S": "0.05"}):
                thread.start()
                first, peer = client.recvfrom(516)
                second, retry_peer = client.recvfrom(516)
                self.assertEqual(second, first)
                self.assertEqual(retry_peer, peer)
                client.sendto(struct.pack("!HH", TFTP.OPCODE_ACK, 1), peer)
                thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_wrong_transfer_id_gets_error_without_retransmission(self):
        peer = ("127.0.0.1", 12345)
        stranger = ("127.0.0.1", 54321)
        ack = struct.pack("!HH", TFTP.OPCODE_ACK, 1)
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "recovery.uImage"
            image.write_bytes(b"x")
            sock = ScriptedSocket([(ack, stranger), (ack, peer)])

            TFTP.serve_file(sock, peer, image)

        data = [
            (packet, target)
            for packet, target in sock.sent
            if packet[:2] == b"\x00\x03"
        ]
        errors = [
            (packet, target)
            for packet, target in sock.sent
            if packet[:2] == b"\x00\x05"
        ]
        self.assertEqual(len(data), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(
            struct.unpack("!HH", errors[0][0][:4]),
            (TFTP.OPCODE_ERROR, 5),
        )
        self.assertEqual(errors[0][1], stranger)

    def test_cancel_while_rejecting_wrong_transfer_id_is_clean(self):
        peer = ("127.0.0.1", 12345)
        stranger = ("127.0.0.1", 54321)
        cancel = threading.Event()
        ack = struct.pack("!HH", TFTP.OPCODE_ACK, 1)

        class ClosingSocket(ScriptedSocket):
            def sendto(self, packet, target):
                if packet[:2] == b"\x00\x05":
                    cancel.set()
                    raise OSError(errno.EBADF, "Bad file descriptor")
                super().sendto(packet, target)

        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "recovery.uImage"
            image.write_bytes(b"recovery")
            sock = ClosingSocket([(ack, stranger)])

            TFTP.serve_file(sock, peer, image, cancel)

    def test_stopping_an_unstarted_transfer_is_clean(self):
        cancel = threading.Event()
        resend = threading.Event()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        thread = threading.Thread()
        transfer = TFTP.ActiveTransfer(
            cancel,
            resend,
            sock,
            thread,
            ("127.0.0.1", 12345),
            Path("recovery.uImage"),
        )

        TFTP.stop_transfer(transfer)

        self.assertTrue(cancel.is_set())
        self.assertEqual(sock.fileno(), -1)


class TftpGuideTests(unittest.TestCase):
    def test_install_guide_uses_explicit_tftp_lifecycle(self):
        guide = (ROOT / "docs/install.md").read_text()

        self.assertNotIn("sudo timeout", guide)
        self.assertNotIn("--timeout=15m", guide)
        self.assertIn("printf 'sudo %q %q %q", guide)
        self.assertIn("close_tftp_firewall", guide)
        self.assertIn("TFTP server stopped", guide)


if __name__ == "__main__":
    unittest.main()
