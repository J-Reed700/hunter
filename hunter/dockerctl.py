"""Minimal Docker Engine API client over the unix socket — stdlib only.

Covers exactly what hunter's job engine needs: container state, log tails, and
exec (attached with output capture, or detached fire-and-forget + inspect).
Everything speaks the raw HTTP API through /var/run/docker.sock; there is no
docker CLI in the image on purpose (nothing to keep in version lockstep).

Every call has a hard timeout so unix-socket reads never hang indefinitely.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
from urllib.parse import quote

_FRAME_HEADER = 8   # non-TTY streams: 8-byte header per frame
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class DockerError(RuntimeError):
    pass


class DockerClient:
    def __init__(self, sock_path: str | None = None, timeout: int = 30):
        self.sock_path = sock_path or os.environ.get(
            "DOCKER_SOCK", "/var/run/docker.sock")
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return os.path.exists(self.sock_path)

    # ---- plumbing ----------------------------------------------------------

    def _conn(self, timeout: int | None = None) -> http.client.HTTPConnection:
        eff = timeout or self.timeout
        conn = http.client.HTTPConnection("localhost", timeout=eff)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(eff)
            sock.connect(self.sock_path)
        except OSError:
            sock.close()
            raise
        conn.sock = sock
        return conn

    def _request(self, method: str, path: str, payload=None,
                 timeout: int | None = None,
                 max_bytes: int = MAX_RESPONSE_BYTES) -> tuple[int, bytes]:
        try:
            conn = self._conn(timeout)
        except OSError as exc:
            raise DockerError(f"docker socket unavailable: {exc}") from exc
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            headers = {"Content-Type": "application/json"} if body else {}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise DockerError(
                    f"docker {method} {path}: response exceeds {max_bytes} bytes")
            return resp.status, data
        except (OSError, http.client.HTTPException) as exc:
            raise DockerError(f"docker {method} {path}: {exc}") from exc
        finally:
            conn.close()

    @staticmethod
    def _demux(raw: bytes) -> str:
        out: list[bytes] = []
        i = 0
        n = len(raw)
        while i + _FRAME_HEADER <= n:
            stream = raw[i]
            if stream not in (0, 1, 2) or raw[i + 1:i + 4] != b"\x00\x00\x00":
                if i == 0:
                    return raw.decode("utf-8", "replace")
                break
            size = int.from_bytes(raw[i + 4:i + 8], "big")
            end = i + _FRAME_HEADER + size
            if end > n:
                # Partial trailing frame: emit payload bytes without leaking 8B header.
                out.append(raw[i + _FRAME_HEADER:])
                i = n
                break
            out.append(raw[i + _FRAME_HEADER:end])
            i = end
        if i == 0 and not out:
            return raw.decode("utf-8", "replace")
        return b"".join(out).decode("utf-8", "replace")

    # ---- container state ---------------------------------------------------

    def inspect(self, name: str) -> dict | None:
        qname = quote(name, safe="")
        status, body = self._request("GET", f"/containers/{qname}/json")
        if status != 200:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def state(self, name: str) -> dict:
        try:
            info = self.inspect(name)
        except DockerError as exc:
            return {"name": name, "exists": False, "running": False,
                    "status": "unreachable", "health": None, "since": None,
                    "error": str(exc)}
        if not info:
            return {"name": name, "exists": False, "running": False,
                    "status": "absent", "health": None, "since": None}
        st = info.get("State", {})
        return {
            "name": name,
            "exists": True,
            "running": bool(st.get("Running")),
            "status": st.get("Status", "?"),
            "health": (st.get("Health") or {}).get("Status"),
            "since": st.get("StartedAt"),
            "exit_code": st.get("ExitCode"),
        }

    def env_of(self, name: str) -> dict[str, str]:
        info = self.inspect(name) or {}
        out: dict[str, str] = {}
        for kv in (info.get("Config") or {}).get("Env") or []:
            k, _, v = kv.partition("=")
            out[k] = v
        return out

    def logs(self, name: str, lines: int = 200,
             max_bytes: int = MAX_RESPONSE_BYTES) -> str:
        qname = quote(name, safe="")
        status, body = self._request(
            "GET",
            f"/containers/{qname}/logs?stdout=1&stderr=1&timestamps=0"
            f"&tail={int(lines)}",
            max_bytes=max_bytes)
        if status != 200:
            raise DockerError(f"logs {name}: HTTP {status}")
        return self._demux(body)

    # ---- exec --------------------------------------------------------------

    def exec_run(self, name: str, cmd: list[str], user: str | None = None,
                 detach: bool = False, timeout: int = 120,
                 env: dict[str, str] | None = None,
                 workdir: str | None = None,
                 max_bytes: int = MAX_RESPONSE_BYTES) -> dict:
        """Run cmd (argv list) inside a container.

        detach=True returns immediately with exit_code None — for jobs that
        outlive an HTTP request. Attached calls capture stdout+stderr and the
        real exit code.
        """
        spec: dict = {"AttachStdout": not detach, "AttachStderr": not detach,
                      "Tty": False, "Cmd": cmd}
        if user and str(user).strip():
            spec["User"] = str(user).strip()
        if env:
            spec["Env"] = [f"{k}={v}" for k, v in env.items()]
        if workdir:
            spec["WorkingDir"] = workdir
        qname = quote(name, safe="")
        status, body = self._request("POST", f"/containers/{qname}/exec",
                                     spec, timeout=timeout)
        if status != 201:
            raise DockerError(
                f"exec create on {name}: HTTP {status} {body[:200]!r}")
        try:
            exec_id = json.loads(body)["Id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DockerError(f"exec create on {name}: invalid response") from exc

        qexec = quote(exec_id, safe="")
        status, body = self._request("POST", f"/exec/{qexec}/start",
                                     {"Detach": detach, "Tty": False},
                                     timeout=timeout, max_bytes=max_bytes)
        if status not in (200, 204):
            raise DockerError(
                f"exec start on {name}: HTTP {status} {body[:200]!r}")
        if detach:
            return {"output": "", "exit_code": None, "exec_id": exec_id}
        out = self._demux(body)
        info = self.exec_inspect(exec_id, timeout=timeout)
        if info.get("unreachable") or info.get("missing") or info.get("invalid"):
            raise DockerError(
                f"exec inspect failed on {name} ({exec_id}): {info}")
        return {"output": out,
                "exit_code": info.get("exit_code"),
                "exec_id": exec_id}

    def exec_inspect(self, exec_id: str, timeout: int | None = None) -> dict:
        qexec = quote(exec_id, safe="")
        try:
            status, info = self._request("GET", f"/exec/{qexec}/json",
                                         timeout=timeout)
        except DockerError:
            return {"running": False, "exit_code": None, "unreachable": True}
        if status != 200:
            return {"running": False, "exit_code": None, "missing": True}
        try:
            d = json.loads(info)
            return {"running": bool(d.get("Running")),
                    "exit_code": d.get("ExitCode"), "pid": d.get("Pid")}
        except json.JSONDecodeError:
            return {"running": False, "exit_code": None, "invalid": True}
