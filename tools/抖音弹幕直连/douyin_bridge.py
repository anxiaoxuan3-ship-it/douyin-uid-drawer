# -*- coding: utf-8 -*-
"""抖音弹幕直连桥接（实验性）

原理
----
     你自己的浏览器（真实设备指纹 / 真实登录态）
              │  打开 https://live.douyin.com/<房间号>
              │  Chrome DevTools Protocol (本机 9222 端口)
              ▼
       本脚本 —— 监听 WebSocket 帧 → 解 protobuf → 提取弹幕
              │  HTTP POST
              ▼
       抖音UID抽号器  (http://127.0.0.1:8787/push)

为什么不用纯 Python 直连抖音 WebSocket？
    抖音的 webcast 握手需要 `signature`（webmssdk 混淆 JS 算出来的 X-Bogus），
    而且有设备/IP 风控。实测在本机直连会直接被 DEVICE_BLOCKED 挡掉。
    借真实浏览器抓帧可以完全绕开签名和设备指纹问题 —— 签名是浏览器自己算的。

用法
----
    python douyin_bridge.py                 # 自动找一个正在直播的房间
    python douyin_bridge.py 123456789       # 指定房间号
    python douyin_bridge.py https://live.douyin.com/123456789
    python douyin_bridge.py --attach        # 连已经开着调试端口的浏览器

依赖：只用 Python 标准库 + 本目录的 wsclient.py / douyin_pb.py。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from douyin_pb import parse_frame                       # noqa: E402
from wsclient import WebSocket, WebSocketError          # noqa: E402

DEBUG_PORT = 9222
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def http_get(url: str, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def http_post_json(url: str, obj: Dict[str, Any], timeout: float = 5.0) -> Optional[dict]:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def find_browser() -> Optional[str]:
    for p in BROWSERS:
        if p and os.path.exists(p):
            return p
    return None


def find_live_rooms(categories=("720", "1", "2", "3", "4", "5", "6", "7")) -> List[str]:
    """从抖音直播分类页收集一批正在直播的房间号。

    （分类页 SSR 里的在线人数客户端才填，拿不到真实人气，所以这里只收集不排序。）
    """
    seen: List[str] = []
    for cat in categories:
        url = f"https://live.douyin.com/category/{cat}"
        try:
            html = http_get(url, timeout=15)
        except Exception:
            continue
        for rid in re.findall(r"live\.douyin\.com/(\d{6,20})", html):
            if rid not in seen:
                seen.append(rid)
    return seen


def normalize_room(arg: str) -> str:
    arg = arg.strip()
    m = re.search(r"live\.douyin\.com/(\d+)", arg)
    if m:
        return m.group(1)
    m = re.search(r"(\d{6,20})", arg)
    if m:
        return m.group(1)
    return arg


# --------------------------------------------------------------------------- #
# CDP
# --------------------------------------------------------------------------- #

class CDP:
    """一个极简的 Chrome DevTools Protocol 客户端。"""

    def __init__(self, ws_url: str) -> None:
        self.ws = WebSocket(ws_url, timeout=15)
        self.ws.settimeout(None)
        self._id = 0
        self._pending: Dict[int, str] = {}

    def call(self, method: str, params: Optional[dict] = None) -> int:
        self._id += 1
        self.ws.send_json({"id": self._id, "method": method,
                           "params": params or {}})
        self._pending[self._id] = method
        return self._id

    def events(self):
        """不断产出 CDP 事件 (method, params)。"""
        while True:
            got = self.ws.recv()
            if got is None:
                return
            opcode, payload = got
            if opcode != 1:
                continue
            try:
                msg = json.loads(payload.decode("utf-8", "replace"))
            except Exception:
                continue
            if "method" in msg:
                yield msg["method"], msg.get("params") or {}
            elif "id" in msg:
                self._pending.pop(msg["id"], None)

    def close(self) -> None:
        self.ws.close()


_LAST_TARGETS_INFO = "（还没查询过）"


def list_targets(port: int = DEBUG_PORT) -> List[dict]:
    global _LAST_TARGETS_INFO
    try:
        raw = http_get(f"http://127.0.0.1:{port}/json/list", timeout=5)
        data = json.loads(raw)
        pages = [t for t in data if t.get("type") == "page"]
        _LAST_TARGETS_INFO = (f"HTTP 200，{len(data)} 个目标，"
                              f"其中 page {len(pages)} 个")
        return data
    except Exception as exc:
        _LAST_TARGETS_INFO = f"{type(exc).__name__}: {exc}"
        return []


def pick_page_target(targets: List[dict], want_sub: str = "douyin",
                     prefer: str = "") -> Optional[dict]:
    """从调试目标里挑一个页面。

    优先挑 URL 里含 ``prefer`` 的（也就是我们真正想要的那个房间），
    其次挑含 ``want_sub`` 的，最后退而求其次拿第一个页面。
    """
    pages = [t for t in targets
             if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        return None
    if prefer:
        for t in pages:
            if prefer in (t.get("url") or ""):
                return t
    for t in pages:
        if want_sub in (t.get("url") or ""):
            return t
    return pages[0]


def wait_for_target(port: int, want_sub: str, timeout: float = 45.0,
                    prefer: str = "") -> Optional[dict]:
    end = time.time() + timeout
    fallback = None
    while time.time() < end:
        t = pick_page_target(list_targets(port), want_sub, prefer)
        if t:
            if prefer and prefer in (t.get("url") or ""):
                return t
            fallback = fallback or t
        time.sleep(0.7)
    return fallback


# --------------------------------------------------------------------------- #
# 推送
# --------------------------------------------------------------------------- #

class Pusher:
    """把弹幕转发给抽号器。断线自动重试，不阻塞主循环。"""

    def __init__(self, base: str, token: str = "") -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.ok = 0
        self.fail = 0
        self._warned = False
        self.target = None          # (pid, port) 当前连上的那台抽号器
        self.pool = 0

    def _url(self, path: str = "/push") -> str:
        url = self.base + path
        if self.token:
            url += ("&" if "?" in url else "?") + "token=" + urllib.parse.quote(self.token)
        return url

    def hello(self) -> bool:
        """先跟抽号器打个招呼，把「连上的是哪一台」说清楚。

        这一步能提前发现最常见的坑：弹幕发给了另一个还开着的抽号器实例，
        表现就是「日志里识别成功，但界面上的参与人数不涨」。
        """
        try:
            with urllib.request.urlopen(self._url("/status"), timeout=6) as r:
                info = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as exc:
            log(f"!! 连不上抽号器（{self.base}）：{exc}")
            log("   请先启动抽号器，并确认「弹幕来源 → ① HTTP 推送」已启用。")
            self._warned = True
            return False

        self.target = (info.get("pid"), info.get("port"))
        self.pool = info.get("pool_size", 0)
        log(f"✓ 已连上抽号器：进程号 {self.target[0]}，端口 {self.target[1]}，"
            f"当前参与池 {self.pool} 人")
        log("  （如果这里显示的端口/人数，和你正在看的那个抽号器界面对不上，"
            "说明弹幕发给了另一个实例 ——")
        log("    那就把多余的「UID抽号器」进程全部结束，只留一个。）")
        return True

    def push(self, text: str, nick: str = "") -> None:
        res = http_post_json(self._url("/push"),
                             {"text": text, "nick": nick, "src": "抖音直连"})
        if res and res.get("ok"):
            self.ok += 1
            # 目标换了（或第一次成功）就报一次，方便发现「推错实例」
            ident = (res.get("pid"), res.get("port"))
            if ident != self.target:
                self.target = ident
                self.pool = res.get("pool", 0)
                log(f"⚠ 现在收弹幕的是进程号 {ident[0]}、端口 {ident[1]} 的抽号器"
                    f"（和刚才那台不是同一个）")
            self.pool = res.get("pool", self.pool)
            if res.get("uid"):
                log(f"  → {res['uid']}  ({nick})  [{res.get('status')}]  "
                    f"池内 {self.pool} 人")
        else:
            self.fail += 1
            if not self._warned:
                self._warned = True
                log(f"  !! 推给抽号器失败，请确认抽号器已启动且"
                    f"「弹幕来源 → HTTP 推送」已启用：{self.base}/push")

    def push_rank(self, rank: Dict[str, Any]) -> None:
        """把在线/贡献榜快照推给抽号器（/rank 接口）。

        榜数据比弹幕稀疏得多，只在确实有内容时才推，避免无谓的请求。
        """
        ranks = rank.get("ranks") or []
        if not ranks:
            return
        res = http_post_json(self._url("/rank"), {"rank": rank, "src": "抖音直连"})
        if res and res.get("ok"):
            self.ok += 1
        else:
            # 榜转发失败不刷屏，静默累计即可（弹幕推送那边会提示主链路）
            self.fail += 1


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def warn_if_profile_fat(profile: str) -> None:
    """配置目录太大就提醒一下 —— 那里面全是缓存，删掉毫无影响。"""
    if not os.path.isdir(profile):
        return
    size = dir_size(profile)
    if size > 300 * 1024 * 1024:
        log(f"提示：浏览器配置目录已经有 {size / 1048576:.0f} MB（全是缓存）。")
        log(f"      不用的时候把它删掉就行，下次会重新建：{profile}")
        log("      （里面没有登录信息，看公开直播间的弹幕也不需要登录）")


def launch_browser(exe: str, url: str, profile: str, port: int,
                   extra: Optional[List[str]] = None) -> subprocess.Popen:
    profile = os.path.abspath(profile)
    try:
        os.makedirs(profile, exist_ok=True)
    except OSError as exc:
        raise SystemExit(
            f"建不了浏览器配置目录：{profile}\n"
            f"  {exc}\n"
            f"换个位置试试：  --profile \"D:\\某处\\浏览器配置\"")
    warn_if_profile_fat(profile)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "--autoplay-policy=no-user-gesture-required",
        "--window-size=1280,860",
        # 让浏览器尽量就是一台普通浏览器：
        # 实测「只开调试端口 + 挂 CDP」并不会让 navigator.webdriver 变成 true
        # （Chrome 只在 --enable-automation 时才置 true，那是 Selenium 的做法），
        # 这里再关掉 AutomationControlled 这个 Blink 特性，把其它自动化特征也一并去掉。
        "--disable-blink-features=AutomationControlled",
        # 限制磁盘缓存。不限制的话这个配置目录会一直长
        # （实测反复跑几十次能涨到 170MB+），而它对我们毫无用处 —— 我们只是旁听。
        "--disk-cache-size=33554432",      # 32 MB
        "--media-cache-size=8388608",      # 8 MB
        "--disable-features=Translate,OptimizationHints",
    ] + list(extra or []) + [url]
    log(f"启动浏览器：{os.path.basename(exe)}")
    creation = 0
    if sys.platform == "win32":
        creation = 0x00000008  # DETACHED_PROCESS
    return subprocess.Popen(args, creationflags=creation,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def close_browser(proc: Optional[subprocess.Popen]) -> None:
    """关掉桥接自己启动的浏览器（连同它的子进程）。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        else:
            proc.terminate()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def port_alive(port: int) -> bool:
    """调试端口上是不是已经有浏览器在跑。"""
    return bool(list_targets(port))


def run(args) -> int:
    explicit_room = bool(args.room)
    rooms: List[str] = []
    if explicit_room:
        rooms = [normalize_room(args.room)]
    else:
        log("没有指定房间号，正在从抖音直播分类页找几个在播的…")
        rooms = find_live_rooms()
        if not rooms:
            log("找不到在播房间。请手动指定：python douyin_bridge.py 123456789")
            return 2
        log(f"找到 {len(rooms)} 个在播房间，先用 {rooms[0]}")

    room_url = f"https://live.douyin.com/{rooms[0]}"
    log(f"目标直播间：{room_url}")

    proc = None
    exe = None
    if not args.attach:
        if port_alive(args.port):
            # 端口上已经有浏览器（多半是上一次桥接留下的），直接用它，别重复启动
            log(f"端口 {args.port} 上已经有浏览器在跑，直接连它（不重复启动）")
        else:
            exe = find_browser()
            if not exe:
                log("没找到 Chrome / Edge。请手动装一个，或者用 --attach 连已开的浏览器。")
                return 2
            proc = launch_browser(exe, room_url, args.profile, args.port,
                                  extra=args.browser_arg)

    # 优先找已经是目标房间的那个标签页；找不到就先拿一个
    target = wait_for_target(args.port, "douyin", timeout=args.launch_timeout,
                             prefer=rooms[0])

    # 常见坑：同配置目录已经有一个浏览器开着（而且它没开调试端口），
    # 这时新启动会「并入旧实例」，端口永远不开。换一个干净目录重试一次。
    if target is None and proc is not None and exe:
        log("等不到调试端口，换个干净的浏览器配置目录再试一次…")
        import tempfile
        alt = os.path.join(tempfile.gettempdir(),
                           f"douyin_bridge_{os.getpid()}")
        log(f"  （临时目录：{alt}）")
        try:
            proc = launch_browser(exe, room_url, alt, args.port,
                                  extra=args.browser_arg)
            target = wait_for_target(args.port, "douyin",
                                     timeout=args.launch_timeout,
                                     prefer=rooms[0])
            if target:
                log("重试成功了。要根治的话：把之前那个桥接的浏览器窗口全部关掉，"
                    "或者用 --profile 指定一个别的目录。")
        except Exception as exc:
            log(f"  重试失败：{exc}")

    if not target:
        log(f"等不到浏览器调试目标（端口 {args.port}）。")
        log(f"  最后一次查 /json/list 的结果：{_LAST_TARGETS_INFO}")
        log(f"  - 上一个桥接的浏览器还开着，占着同一个配置目录：{args.profile}")
        log("    → 把那些浏览器窗口全部关掉（或者重启电脑），再重试")
        log("  - 换个配置目录试试： --profile \"D:\\某个新目录\"")
        log("  - 或者手动开好浏览器后用 --attach 连它")
        return 3
    log(f"已连上页面：{target.get('url', '')[:90]}")

    cdp = CDP(target["webSocketDebuggerUrl"])
    cdp.call("Network.enable", {"maxPostDataSize": 65536})
    cdp.call("Page.enable")

    # 关键：强制导航到目标房间。
    # 浏览器配置目录是复用的，Chrome 可能恢复上一次的标签页，
    # 不强制导航的话会「以为在看 A 房间，其实连的是 B 房间」。
    if rooms[0] not in (target.get("url") or ""):
        log(f"当前标签页不是目标房间，导航到 {room_url}")
        try:
            cdp.call("Page.navigate", {"url": room_url})
            time.sleep(2.5)
        except Exception as exc:
            log(f"  导航失败：{exc}")

    pusher = Pusher(args.push_to, args.token)
    pusher.hello()

    # 记录哪些 requestId 是抖音的弹幕 WebSocket
    ws_urls: Dict[str, str] = {}
    stats = {"frames": 0, "chat": 0, "other": 0, "acked": 0}
    bad = 0
    last_report = time.time()
    last_chat = time.time()
    room_started = time.time()
    saw_ws = False
    warned_no_ws = False
    current_url = target.get("url", "")
    rotate = (not explicit_room) and args.auto_rotate and len(rooms) > 1
    room_idx = 0
    rotate_after = max(20.0, args.rotate_after)
    ws_grace = 15.0          # 多久还没连上弹幕通道就认为这个房间是死的
    dead_rooms = 0

    def next_room(reason: str) -> None:
        """换到下一个候选房间。"""
        nonlocal room_idx, last_chat, last_report, room_started
        nonlocal saw_ws, warned_no_ws, dead_rooms
        room_idx = (room_idx + 1) % len(rooms)
        nxt = f"https://live.douyin.com/{rooms[room_idx]}"
        log(f"{reason} → 换到 {rooms[room_idx]}")
        try:
            cdp.call("Page.navigate", {"url": nxt})
        except Exception as exc:
            log(f"  跳转失败：{exc}")
        last_chat = last_report = room_started = time.time()
        saw_ws = False
        warned_no_ws = False

    if rotate:
        log(f"没指定房间号：{ws_grace:.0f} 秒连不上弹幕通道、"
            f"或 {rotate_after:.0f} 秒没弹幕，就自动换下一个房间")
    else:
        log("提示：用你自己的直播间号最稳 —— python douyin_bridge.py 你的房间号")

    log("开始监听弹幕…（Ctrl+C 停止）")
    try:
        for method, params in cdp.events():
            if method == "Network.webSocketCreated":
                rid = params.get("requestId", "")
                ws_urls[rid] = params.get("url", "")
                if "webcast" in ws_urls[rid]:
                    if not saw_ws:
                        saw_ws = True
                    log(f"  发现弹幕通道：{ws_urls[rid][:100]}")

            elif method == "Page.frameNavigated":
                fr = params.get("frame") or {}
                if not fr.get("parentId"):          # 只看主框架
                    current_url = fr.get("url", "") or current_url
                    if current_url.startswith("http") and \
                            "live.douyin.com" not in current_url:
                        log(f"  页面被跳到 {current_url[:70]} —— "
                            f"这个房间多半没在直播")

            elif method == "Network.webSocketFrameReceived":
                rid = params.get("requestId", "")
                url = ws_urls.get(rid, "")
                if url and "webcast" not in url and "douyin" not in url:
                    continue
                resp = params.get("response") or {}
                if resp.get("opcode") != 2:
                    continue
                data = resp.get("payloadData") or ""
                try:
                    raw = base64.b64decode(data)
                except Exception:
                    continue

                stats["frames"] += 1
                try:
                    chats, info = parse_frame(raw)
                except Exception:
                    bad += 1
                    continue

                if not info.get("payload_type"):
                    stats["other"] += 1
                    continue

                if chats:
                    last_chat = time.time()
                for c in chats:
                    stats["chat"] += 1
                    log(f"弹幕  {c['nick']}：{c['content']}")
                    pusher.push(c["content"], c["nick"])

                # 在线/贡献榜快照（WebcastRoomUserSeqMessage）
                if info.get("rank"):
                    pusher.push_rank(info["rank"])

                if info.get("need_ack") and info.get("internal_ext"):
                    stats["acked"] += 1

            now = time.time()
            if now - last_report > 30:
                last_report = now
                log(f"… 收到 {stats['frames']} 帧 / {stats['chat']} 条弹幕 / "
                    f"已转发 {pusher.ok} 条（失败 {pusher.fail}）")

            # ---- 这个房间是不是死的（页面被跳走 / 压根没连上弹幕通道）---- #
            if not saw_ws and now - room_started > ws_grace:
                if rotate:
                    dead_rooms += 1
                    next_room(f"房间 {rooms[room_idx]} 连不上弹幕通道")
                    if dead_rooms > 40:
                        log("换了太多房间都不行，可能抖音改版了，先停下。")
                        break
                    continue
                if not warned_no_ws:
                    warned_no_ws = True
                    log("  ！已经 15 秒还没连上弹幕通道。可能的原因：")
                    log("    - 这个房间没在直播（页面被跳走了）")
                    log("    - 页面还没加载完，或者抖音弹了验证码/登录框")
                    log(f"    - 当前页面：{current_url[:70]}")

            # ---- 房间是活的但没人说话 ---- #
            if rotate and saw_ws and now - last_chat > rotate_after:
                next_room(f"房间 {rooms[room_idx]} {rotate_after:.0f} 秒没弹幕")
                last_report = now
    except KeyboardInterrupt:
        log("用户中断")
    except WebSocketError as exc:
        log(f"连接断了：{exc}")
    except Exception as exc:
        log(f"出错：{type(exc).__name__}: {exc}")
    finally:
        try:
            cdp.close()
        except Exception:
            pass
        if proc is not None and not args.keep_browser:
            log("关闭桥接启动的浏览器（想留着就加 --keep-browser）")
            close_browser(proc)

    log(f"结束。共 {stats['frames']} 帧、{stats['chat']} 条弹幕、"
        f"转发成功 {pusher.ok} 条、转发失败 {pusher.fail} 条")
    if pusher.fail and not pusher.ok:
        log("一条都没转发成功 —— 请确认抽号器已启动，且"
            "「弹幕来源」里勾了「启用弹幕接收服务」。")
    return 0


def detect_app_url() -> Tuple[str, str]:
    """从抽号器的配置文件里读出 HTTP 端口和 token，省得用户手填。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = os.path.join(os.path.dirname(os.path.dirname(here)), "数据", "config.json")
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            d = json.load(f)
        src = d.get("source", {}) or {}
        port = int(src.get("http_port") or 8787)
        return f"http://127.0.0.1:{port}", str(src.get("http_token") or "")
    except Exception:
        return "http://127.0.0.1:8787", ""


def main() -> int:
    ap = argparse.ArgumentParser(
        description="抖音弹幕直连桥接（借真实浏览器抓帧，转发给抽号器）")
    ap.add_argument("room", nargs="?", default="",
                    help="直播间号或完整网址；不填会自动找一个在播房间")
    ap.add_argument("--push-to", default="",
                    help="抽号器的接收地址；不填会自动从抽号器的配置里读")
    ap.add_argument("--token", default="", help="抽号器设置的 token（如果设了）")
    ap.add_argument("--port", type=int, default=DEBUG_PORT,
                    help="浏览器调试端口，默认 9222")
    ap.add_argument("--attach", action="store_true",
                    help="不启动浏览器，直接连已经在跑的调试端口")
    ap.add_argument("--profile",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "浏览器配置"),
                    help="浏览器专用配置目录（默认在本脚本旁边的「浏览器配置」，"
                         "跟你日常的浏览器完全隔离）")
    ap.add_argument("--launch-timeout", type=float, default=45.0,
                    help="等浏览器起来的最长秒数")
    ap.add_argument("--browser-arg", action="append", default=[],
                    help="额外传给浏览器的参数，可以重复写多次")
    ap.add_argument("--keep-browser", action="store_true",
                    help="桥接退出后不要关掉它启动的浏览器（默认会关掉，"
                         "少一个长期挂着的自动化会话）")
    ap.add_argument("--auto-rotate", action="store_true", default=True,
                    help="没指定房间号时，某个房间一直没弹幕就自动换下一个（默认开）")
    ap.add_argument("--no-auto-rotate", dest="auto_rotate", action="store_false",
                    help="关掉自动换房间")
    ap.add_argument("--rotate-after", type=float, default=60.0,
                    help="多少秒没弹幕就换房间，默认 60")
    args = ap.parse_args()
    if not args.push_to or not args.token:
        url, token = detect_app_url()
        args.push_to = args.push_to or url
        args.token = args.token or token
        log(f"从抽号器配置读到接收地址：{args.push_to}")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
