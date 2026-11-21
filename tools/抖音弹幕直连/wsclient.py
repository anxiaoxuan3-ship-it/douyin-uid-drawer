# -*- coding: utf-8 -*-
"""极简 WebSocket 客户端（纯标准库，不装任何第三方包）。

只用它连本机的 Chrome DevTools 调试端口（ws://127.0.0.1:9222/...），
所以实现可以很薄：握手 + 帧收发 + 分片重组就够。

参考 RFC 6455。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import urllib.parse
from typing import List, Optional, Tuple

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(Exception):
    pass


class WebSocket:
    """阻塞式 WebSocket 客户端（够用就好）。"""

    def __init__(self, url: str, timeout: float = 10.0,
                 origin: Optional[str] = None) -> None:
        self.url = url
        self.timeout = timeout
        self._buf = b""
        self._closed = False

        u = urllib.parse.urlparse(url)
        if u.scheme not in ("ws", "wss"):
            raise WebSocketError(f"不是 ws/wss 地址：{url}")
        self.secure = u.scheme == "wss"
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if self.secure else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        raw = socket.create_connection((host, port), timeout=timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.secure:
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        self.sock = raw

        key = base64.b64encode(os.urandom(16)).decode()
        headers = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if origin:
            headers.append(f"Origin: {origin}")
        req = ("\r\n".join(headers) + "\r\n\r\n").encode()

        self.sock.sendall(req)
        self._handshake(key)

    # ------------------------------------------------------------------ #

    def _recv_until(self, marker: bytes, limit: int = 65536) -> bytes:
        while marker not in self._buf:
            if len(self._buf) > limit:
                raise WebSocketError("握手响应头过大")
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("握手时连接被关闭")
            self._buf += chunk
        head, _, rest = self._buf.partition(marker)
        self._buf = rest
        return head + marker

    def _handshake(self, key: str) -> None:
        head = self._recv_until(b"\r\n\r\n")
        text = head.decode("latin-1")
        status = text.split("\r\n", 1)[0]
        if "101" not in status:
            raise WebSocketError(f"握手失败：{status}")
        expect = base64.b64encode(
            hashlib.sha1(key.encode() + GUID).digest()).decode()
        if expect.lower() not in text.lower():
            raise WebSocketError("Sec-WebSocket-Accept 校验失败")

    # ------------------------------------------------------------------ #

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise WebSocketError("连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray()
        header.append(0x80 | opcode)           # FIN + opcode
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_json(self, obj) -> None:
        import json
        self.send_text(json.dumps(obj, separators=(",", ":")))

    def recv(self) -> Optional[Tuple[int, bytes]]:
        """收一条完整消息。返回 (opcode, payload)；连接关闭返回 None。"""
        frags: List[bytes] = []
        frag_op = None
        while True:
            b1, b2 = self._read_exact(2)
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]

            mask = self._read_exact(4) if masked else b""
            data = self._read_exact(length) if length else b""
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

            if opcode == OP_CLOSE:
                self._closed = True
                return None
            if opcode == OP_PING:
                self._send_frame(OP_PONG, data)
                continue
            if opcode == OP_PONG:
                continue

            if opcode == OP_CONT:
                frags.append(data)
            else:
                frag_op = opcode
                frags = [data]

            if fin:
                return frag_op, b"".join(frags)

    def settimeout(self, t: Optional[float]) -> None:
        self.sock.settimeout(t)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._send_frame(OP_CLOSE, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
