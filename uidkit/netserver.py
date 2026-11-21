# -*- coding: utf-8 -*-
"""内置 HTTP 服务：接收弹幕推送 + 给浏览器源提供透明叠加页。

这是最通用、最稳的接入方式 —— 任何能发 HTTP 请求的工具（第三方弹幕助手、
Python/Node 小脚本、手机浏览器书签、StreamDeck、OBS 浏览器源）都能把弹幕推进来。

接口一览（默认 http://127.0.0.1:8787）
--------------------------------------
    GET  /                    使用说明小页面（浏览器打开就能看）
    GET  /overlay             浏览器源透明叠加页（给直播伴侣/OBS 的浏览器源用）
    GET  /push?text=...       推送一条弹幕（最方便，浏览器/手机都能用）
    POST /push                推送一条弹幕；body 可以是纯文本，也可以是 JSON
                              {"text":"...", "nick":"...", "uid":"..."}
    POST /push/batch          JSON 数组，一次推多条
    POST /rank                接收在线/贡献榜快照（抖音直连推送）
    GET  /status              当前状态 JSON
    GET  /events              SSE 实时事件流（叠加页用）
    POST /draw?count=1        触发一次抽取
    POST /undo?n=1            撤销中奖
    POST /reset               清空参与池
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .config import AppConfig
from .state import Session
from .web import INFO_PAGE, OVERLAY_PAGE

MAX_BODY = 1 << 20  # 1MB


class _Server(ThreadingHTTPServer):
    """抽号器自己的 HTTP 服务。

    ⚠️ 必须关掉 ``SO_REUSEADDR``。

    ``http.server.HTTPServer`` 默认把 ``allow_reuse_address`` 设成 1，而在
    **Windows 上 SO_REUSEADDR 的语义是「允许另一个 socket 绑同一个端口」**
    （跟 Linux 相反，Linux 上它只管 TIME_WAIT）。

    后果非常阴险：第二个抽号器实例**能成功绑上同一个端口**，它以为自己占着 8787，
    实际上连接全被第一个实例接走了 —— 于是「日志里识别成功，但参与人数不涨」，
    而且它自己毫无察觉，既不换端口也不报警。

    关掉之后第二个实例会老老实实拿到 WinError 10048，
    走更换端口 + 醒目提示的流程。
    """

    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 64


def probe_existing_app(host: str, port: int, timeout: float = 1.5) -> Optional[dict]:
    """看看这个端口上是不是已经有一个抽号器在跑；是的话返回它的 /status。"""
    if not port:
        return None
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    try:
        url = f"http://{host}:{port}/status"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "UIDLottery/1.0"

    # 由 NetServer 注入
    session: Session = None          # type: ignore[assignment]
    cfg: AppConfig = None            # type: ignore[assignment]

    # ---- 基础设施 ------------------------------------------------------ #

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # 静音：默认的访问日志会污染界面

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _text(self, s: str, code: int = 200) -> None:
        self._send(code, s.encode("utf-8"), "text/plain; charset=utf-8")

    def _html(self, s: str, code: int = 200) -> None:
        self._send(code, s.encode("utf-8"), "text/html; charset=utf-8")

    # ---- 鉴权 ---------------------------------------------------------- #

    def _is_local(self) -> bool:
        """请求是不是从本机发出来的。"""
        try:
            addr = (self.client_address or ("",))[0]
        except Exception:
            return False
        if not addr:
            return False
        return addr in ("::1", "localhost") or addr.startswith("127.")

    def _auth_ok(self, q: dict) -> bool:
        token = (self.cfg.source.http_token or "").strip()
        if not token:
            return True

        # 本机访问一律放行。
        #   * token 的用途是挡住局域网里别人乱推弹幕；
        #   * 本机上的程序本来就能直接读到 数据\config.json，token 就明文写在里面，
        #     所以对本机设防没有任何意义 —— 只会把自带的「接口面板」和
        #     「浏览器叠加页」挡在门外（它们打开时不会带 token）。
        if self._is_local():
            return True

        given = (q.get("token", [""])[0]
                 or self.headers.get("X-Token", "")
                 or "")
        return given == token

    def _deny(self) -> None:
        self._json({
            "ok": False,
            "error": "token 不对",
            "hint": ("本机（127.0.0.1）访问不需要 token；"
                     "从别的设备访问请带上 ?token=你的token，"
                     "或者到「弹幕来源」页把 token 清空（只在本机用的话不需要它）。"),
        }, 403)

    # ---- 路由 ---------------------------------------------------------- #

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        q = urllib.parse.parse_qs(parsed.query)

        if not self._auth_ok(q):
            return self._deny()

        if path == "/":
            return self._html(INFO_PAGE)
        if path == "/overlay":
            return self._html(OVERLAY_PAGE)
        if path == "/status":
            snap = self.session.snapshot()
            snap["pid"] = os.getpid()
            snap["port"] = self._my_port()
            return self._json(snap)
        if path == "/events":
            return self._sse()
        if path == "/push":
            text = (q.get("text", [""])[0] or q.get("t", [""])[0])
            nick = (q.get("nick", [""])[0] or q.get("n", [""])[0])
            return self._push(text, nick, "http")
        if path == "/ping":
            return self._json({"ok": True, "time": time.time()})

        return self._json({"ok": False, "error": f"没有这个接口: {path}"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        q = urllib.parse.parse_qs(parsed.query)

        if not self._auth_ok(q):
            return self._deny()

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(min(length, MAX_BODY)) if length > 0 else b""
        body = raw.decode("utf-8", "replace").strip()

        if path == "/push":
            return self._handle_push_body(body, q)
        if path == "/push/batch":
            return self._handle_batch(body, q)
        if path == "/rank":
            return self._handle_rank(body, q)
        if path == "/draw":
            count = _int(q.get("count", [None])[0], self.session.cfg.draw_count)
            if self.session.animating:
                return self._json(
                    {"ok": False, "error": "上一轮还在滚动，等一下再抽",
                     "animating": True}, 409)
            res = self.session.do_draw(count)
            if res is None:
                return self._json(
                    {"ok": False, "error": "参与池是空的，或者所有人都已经中过奖了"},
                    409)
            return self._json({"ok": True, "round": res.round_no,
                               "uids": res.winner_uids, "seed": res.seed,
                               "fingerprint": res.fingerprint})
        if path == "/undo":
            n = _int(q.get("n", ["1"])[0], 1)
            return self._json({"ok": True, "undone": self.session.undo_draw(n)})
        if path == "/reset":
            self.session.reset_pool(keep_winners=True)
            return self._json({"ok": True})

        return self._json({"ok": False, "error": f"没有这个接口: {path}"}, 404)

    # ---- 推送处理 ------------------------------------------------------ #

    def _handle_push_body(self, body: str, q: dict) -> None:
        nick = (q.get("nick", [""])[0] or q.get("n", [""])[0])
        if not body:
            # 允许 POST /push?text=...&nick=... （body 留空）
            text = (q.get("text", [""])[0] or q.get("t", [""])[0])
            if text:
                return self._push(text, nick, "http")
            return self._json({"ok": False, "error": "body 是空的"}, 400)

        if body.startswith("{") or body.startswith("["):
            try:
                obj = json.loads(body)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                text = str(obj.get("text") or obj.get("content")
                           or obj.get("msg") or obj.get("danmaku") or "")
                nick = str(obj.get("nick") or obj.get("nickname")
                           or obj.get("user") or nick or "")
                uid = str(obj.get("uid") or "")
                src = str(obj.get("src") or obj.get("source") or "http")
                return self._push(text, nick, src, uid_hint=uid)
            if isinstance(obj, list):
                return self._handle_batch(body, q)

        return self._push(body, nick, "http")

    def _handle_rank(self, body: str, q: dict) -> None:
        """接收抖音直连推送的在线/贡献榜快照。

        body 是 JSON：{"rank": {"ranks": [...], "total": ..., "popularity": ...}}
        """
        if not body:
            return self._json({"ok": False, "error": "body 是空的"}, 400)
        try:
            obj = json.loads(body)
        except Exception:
            return self._json({"ok": False, "error": "需要 JSON"}, 400)
        rank = obj.get("rank") if isinstance(obj, dict) else None
        if not isinstance(rank, dict):
            return self._json({"ok": False, "error": "缺少 rank 字段"}, 400)
        self.session.apply_rank(rank)
        return self._json({
            "ok": True,
            "total": rank.get("total"),
            "rank_count": len(rank.get("ranks") or []),
            "pid": os.getpid(),
            "port": self._my_port(),
        })

    def _handle_batch(self, body: str, q: dict) -> None:
        try:
            arr = json.loads(body)
        except Exception:
            return self._json({"ok": False, "error": "batch 需要 JSON 数组"}, 400)
        if not isinstance(arr, list):
            arr = [arr]
        n = 0
        for item in arr[:5000]:
            if isinstance(item, str):
                text, nick = item, ""
            elif isinstance(item, dict):
                text = str(item.get("text") or item.get("content") or "")
                nick = str(item.get("nick") or "")
            else:
                continue
            rec = self.session.feed(text, nick=nick, source="http")
            if rec and rec.uid:
                n += 1
        return self._json({"ok": True, "accepted": n, "total": len(arr)})

    def _push(self, text: str, nick: str, source: str,
              uid_hint: str = "") -> None:
        if not text and not uid_hint:
            return self._json({"ok": False, "error": "没有 text"}, 400)
        rec = self.session.feed(text or uid_hint, nick=nick, source=source)
        if rec is None:
            return self._json({"ok": False}, 400)
        # 把「是哪台抽号器收下的」一并回给推送方。
        # 这样抖音直连的窗口里会打印出 pid / 端口 / 池内人数，
        # 万一推给了另一个实例，一眼就能看出来。
        return self._json({
            "ok": True, "uid": rec.uid, "status": rec.status,
            "reason": rec.reason, "pool": len(self.session.pool),
            "pid": os.getpid(), "port": self._my_port(),
        })

    def _my_port(self) -> int:
        try:
            return int(self.server.server_address[1])
        except Exception:
            return 0

    # ---- SSE ----------------------------------------------------------- #

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

        last = None
        try:
            while not self.server.stop_flag.is_set():  # type: ignore[attr-defined]
                snap = self.session.snapshot()
                payload = json.dumps(snap, ensure_ascii=False)
                if payload != last:
                    last = payload
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                else:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def _int(v: Optional[str], default: int) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


class NetServer:
    """把 ThreadingHTTPServer 包一层，方便启停。"""

    def __init__(self, session: Session, cfg: AppConfig) -> None:
        self.session = session
        self.cfg = cfg
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.error: str = ""
        self.port: int = 0
        self.requested_port: int = int(cfg.source.http_port or 8787)
        self.conflict: bool = False
        """True = 想用的端口被占用，只好临时换了别的端口。"""

    @property
    def running(self) -> bool:
        return self.httpd is not None

    def url(self, path: str = "") -> str:
        host = self.cfg.source.http_host
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"http://{host}:{self.port}{path}"

    def start(self) -> bool:
        if self.httpd is not None:
            return True

        handler = type("_BoundHandler", (_Handler,), {
            "session": self.session,
            "cfg": self.cfg,
        })

        host = self.cfg.source.http_host or "127.0.0.1"
        try:
            port = int(self.cfg.source.http_port)
        except (TypeError, ValueError):
            port = 8787
        if port < 0:
            port = 8787
        self.requested_port = port
        self.conflict = False
        self.existing: Optional[dict] = None

        # 绑之前先问一句：这个端口上是不是已经有一个抽号器了？
        # （关掉 SO_REUSEADDR 之后即使不问也能靠绑定失败发现，
        #   但问一下能拿到对方的 pid/人数，提示可以写得具体得多）
        if port:
            found = probe_existing_app(host, port)
            if found is not None:
                self.existing = found
                self.conflict = True
                port = 0            # 别去抢，直接换一个空闲端口

        try:
            httpd = _Server((host, port), handler)
        except OSError as exc:
            if port == 0 or getattr(exc, "errno", None) in (10048, 98, 13, 10013):
                # 端口被占用 / 没权限 → 换一个随机端口，但一定要说清楚
                try:
                    httpd = _Server((host, 0), handler)
                except OSError as exc2:
                    self.error = f"HTTP 服务启动失败：{exc2}"
                    self.session.bus.emit("error", self.error)
                    return False
                self.conflict = True
                self.session.bus.emit(
                    "error",
                    f"端口 {self.requested_port} 被占用了（多半是上一次的程序还没退出）。"
                    f"已经临时改用端口 {httpd.server_address[1]} —— "
                    f"推送弹幕和抖音直连请以界面显示的地址为准。")
            else:
                self.error = f"HTTP 服务启动失败：{exc}"
                self.session.bus.emit("error", self.error)
                return False
        else:
            if self.conflict:
                self.session.bus.emit(
                    "error",
                    f"端口 {self.requested_port} 上已经有一个抽号器在跑，"
                    f"本程序改用端口 {port}。")

        httpd.daemon_threads = True
        httpd.stop_flag = threading.Event()  # type: ignore[attr-defined]
        self.httpd = httpd
        self.port = httpd.server_address[1]
        self.thread = threading.Thread(target=httpd.serve_forever,
                                       kwargs={"poll_interval": 0.3},
                                       name="http-server", daemon=True)
        self.thread.start()
        self.error = ""
        self.session.log(f"弹幕接收服务已启动：{self.url('/')}")
        return True

    def stop(self) -> None:
        if self.httpd is None:
            return
        try:
            self.httpd.stop_flag.set()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        self.httpd = None
        self.thread = None

    def restart(self) -> bool:
        self.stop()
        return self.start()


def local_ip() -> str:
    """拿本机局域网 IP，方便用手机推送弹幕。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
