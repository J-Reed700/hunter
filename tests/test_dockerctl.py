from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from hunter.dockerctl import DockerClient, DockerError


class DockerCtlTests(unittest.TestCase):
    def test_demux_handles_valid_truncated_and_plain(self) -> None:
        # Valid multi-frame stdout + stderr
        f1 = b"\x01\x00\x00\x00" + (5).to_bytes(4, "big") + b"hello"
        f2 = b"\x02\x00\x00\x00" + (6).to_bytes(4, "big") + b" world"
        self.assertEqual(DockerClient._demux(f1 + f2), "hello world")

        # Truncated trailing frame must NOT leak 8-byte binary header
        trunc = f1 + b"\x01\x00\x00\x00" + (50).to_bytes(4, "big") + b"part"
        out = DockerClient._demux(trunc)
        self.assertEqual(out, "hellopart")
        self.assertNotIn("\x01", out)

        # Truncated 4-byte trailing header must NOT leak bytes
        trunc_hdr = f1 + b"\x01\x00\x00\x00"
        self.assertEqual(DockerClient._demux(trunc_hdr), "hello")

        # Plain non-multiplexed text
        self.assertEqual(DockerClient._demux(b"plain output\n"), "plain output\n")

    def test_conn_closes_socket_on_connect_oserror(self) -> None:
        fake_sock = mock.MagicMock()
        fake_sock.connect.side_effect = OSError("refused")
        dc = DockerClient("/nonexistent.sock")
        with mock.patch("hunter.dockerctl.socket.socket", return_value=fake_sock):
            with self.assertRaises(OSError):
                dc._conn()
        fake_sock.close.assert_called_once()

    def test_url_quoting_and_response_cap_and_exec_inspect_error(self) -> None:
        dc = DockerClient("/tmp/unused.sock")
        seen_paths: list[tuple[str, str, int | None]] = []

        def fake_req(method, path, payload=None, timeout=None, max_bytes=16 * 1024 * 1024):
            seen_paths.append((method, path, timeout))
            if path.endswith("/exec"):
                return 201, json.dumps({"Id": "id/with space"}).encode()
            if path.endswith("/start"):
                return 200, b"ok"
            if path.endswith("/json") and "/exec/" in path:
                return 500, b"{}"
            return 200, b"{}"

        with mock.patch.object(dc, "_request", side_effect=fake_req):
            with self.assertRaises(DockerError):
                dc.exec_run("my/container", ["echo", "hi"], timeout=42)

        self.assertEqual(seen_paths[0], ("POST", "/containers/my%2Fcontainer/exec", 42))
        self.assertEqual(seen_paths[1], ("POST", "/exec/id%2Fwith%20space/start", 42))
        self.assertEqual(seen_paths[2], ("GET", "/exec/id%2Fwith%20space/json", 42))

    def test_request_caps_oversized_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = str(Path(tmp) / "docker.sock")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(sock_path)
            srv.listen(1)

            def serve() -> None:
                conn, _ = srv.accept()
                conn.recv(4096)
                body = b"x" * 1024
                resp = (
                    b"HTTP/1.1 200 OK\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )
                conn.sendall(resp)
                conn.close()
                srv.close()

            t = threading.Thread(target=serve, daemon=True)
            t.start()
            dc = DockerClient(sock_path)
            with self.assertRaises(DockerError):
                dc._request("GET", "/version", max_bytes=128)
            t.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
