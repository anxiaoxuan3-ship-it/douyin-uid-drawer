# -*- coding: utf-8 -*-
"""抖音弹幕 protobuf 的最小解析器（手写 varint，不需要 protoc）。

只解析我们真正要用的几层结构，字段号是实测出来的：

    PushFrame    1=seq_id(varint)  2=log_id(varint)  7=payload_type(str)
                 8=payload(bytes, 通常是 gzip 压缩的 Response)
    Response     1=messages(repeated bytes)  5=internal_ext(str)  9=need_ack(varint)
    Message      1=method(str)  2=payload(bytes)
    ChatMessage  2=user(bytes)  3=content(str)
    User         1=id(varint)   3=nick_name(str)

其它 method（礼物、点赞、进房…）也会被识别出来，但我们只关心 ChatMessage。
"""

from __future__ import annotations

import gzip
import zlib
from typing import Any, Dict, Iterator, List, Optional, Tuple

WIRE_VARINT = 0
WIRE_64 = 1
WIRE_LEN = 2
WIRE_32 = 5


def read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    """读一个 varint，返回 (值, 新位置)。"""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("varint 越界")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint 太长")


def iter_fields(buf: bytes) -> Iterator[Tuple[int, int, Any]]:
    """遍历一层 protobuf，产出 (字段号, wire类型, 值)。

    wire 0 → int；wire 2 → bytes；wire 1/5 → bytes（原样）。
    """
    pos = 0
    n = len(buf)
    while pos < n:
        key, pos = read_varint(buf, pos)
        field_no = key >> 3
        wire = key & 0x07
        if field_no == 0:
            return
        if wire == WIRE_VARINT:
            val, pos = read_varint(buf, pos)
            yield field_no, wire, val
        elif wire == WIRE_LEN:
            length, pos = read_varint(buf, pos)
            if pos + length > n:
                return
            yield field_no, wire, buf[pos:pos + length]
            pos += length
        elif wire == WIRE_64:
            if pos + 8 > n:
                return
            yield field_no, wire, buf[pos:pos + 8]
            pos += 8
        elif wire == WIRE_32:
            if pos + 4 > n:
                return
            yield field_no, wire, buf[pos:pos + 4]
            pos += 4
        else:
            return  # 未知 wire 类型，放弃这一层


def to_dict(buf: bytes) -> Dict[int, Any]:
    """把一层解析成 {字段号: 值}。同号字段只保留最后一个。"""
    out: Dict[int, Any] = {}
    for no, _wire, val in iter_fields(buf):
        out[no] = val
    return out


# --------------------------------------------------------------------------- #
# 业务层
# --------------------------------------------------------------------------- #

def _maybe_gunzip(data: bytes) -> bytes:
    if len(data) < 2:
        return data
    if data[:2] == b"\x1f\x8b":                      # gzip 魔数
        try:
            return gzip.decompress(data)
        except Exception:
            try:
                return zlib.decompress(data, 16 + zlib.MAX_WBITS)
            except Exception:
                return data
    if data[:1] == b"\x78":                          # zlib
        try:
            return zlib.decompress(data)
        except Exception:
            return data
    return data


def parse_user(buf: bytes) -> Dict[str, str]:
    """User → {uid, nick}。

    兼容两种 User 结构的字段号差异：
      * ChatMessage 里的 User：nick_name = 3
      * Data.User（RoomUserSeqMessage 等）：user_id = 1, nick_name = 2
    昵称同时探测字段 2 和 3，谁有值用谁。
    """
    d = to_dict(buf)
    uid = d.get(1)
    nick = d.get(3) if isinstance(d.get(3), bytes) else d.get(2)
    return {
        "uid": str(uid) if isinstance(uid, int) else "",
        "nick": nick.decode("utf-8", "replace") if isinstance(nick, bytes) else "",
    }


def parse_chat_message(buf: bytes) -> Dict[str, str]:
    """ChatMessage → {uid, nick, content}。"""
    d = to_dict(buf)
    user = d.get(2)
    content = d.get(3)
    out = {"uid": "", "nick": "", "content": ""}
    if isinstance(user, bytes):
        out.update(parse_user(user))
    if isinstance(content, bytes):
        out["content"] = content.decode("utf-8", "replace")
    return out


def parse_user_seq(buf: bytes) -> Dict[str, Any]:
    """RoomUserSeqMessage → 在线/贡献榜快照。

    字段（实测自 douyin.proto）：

        RoomUserSeqMessage {
          1 common (Common)
          2 ranks  (repeated Contributor)   ← 观众榜，按 rank 排序
          3 total  (uint64)                 ← 总在线人数
          6 popularity (uint64)             ← 人气值
          Contributor {
            1 score (uint64)                ← 贡献分（送礼/打赏加权分，即“礼物金额”数字）
            2 user  (Data.User{1=user_id,2=nick_name,3=avatar})
            3 rank  (uint64)                ← 排名
            4 delta (uint64)                ← 名次变化
            5 is_hidden (bool)
            6 score_description (string)    ← 抖音格式化好的显示文本（如“1.2万”）
            7 exactly_score (string)
          }
        }

    返回 {ranks: [...], total, popularity}。ranks 里的每条都带 uid/nick/rank/score。
    """
    d = to_dict(buf)

    def _contributor(raw: bytes) -> Dict[str, Any]:
        cd = to_dict(raw)
        user = cd.get(2)
        uid, nick = "", ""
        if isinstance(user, bytes):
            u = parse_user(user)
            uid, nick = u["uid"], u["nick"]
        score = cd.get(1)
        rank = cd.get(3)
        return {
            "uid": uid,
            "nick": nick,
            "rank": rank if isinstance(rank, int) else 0,
            "score": score if isinstance(score, int) else 0,
            "score_desc": (cd.get(6).decode("utf-8", "replace")
                           if isinstance(cd.get(6), bytes) else ""),
        }

    ranks: List[Dict[str, Any]] = []
    for no, _wire, val in iter_fields(buf):
        if no == 2 and isinstance(val, bytes):
            ranks.append(_contributor(val))

    total = d.get(3)
    popularity = d.get(6)
    return {
        "ranks": ranks,
        "total": total if isinstance(total, int) else 0,
        "popularity": popularity if isinstance(popularity, int) else 0,
    }


def parse_response(buf: bytes) -> Tuple[List[Dict[str, str]], str, bool, Dict[str, Any]]:
    """Response → (聊天消息列表, internal_ext, need_ack, 榜快照)。

    榜快照来自 WebcastRoomUserSeqMessage（在线/贡献榜）。没有该消息时返回空 dict。
    """
    chats: List[Dict[str, str]] = []
    internal_ext = ""
    need_ack = False
    rank: Dict[str, Any] = {}

    for no, _wire, val in iter_fields(buf):
        if no == 1 and isinstance(val, bytes):
            md = to_dict(val)
            method = md.get(1)
            payload = md.get(2)
            if not isinstance(method, bytes) or not isinstance(payload, bytes):
                continue
            name = method.decode("utf-8", "replace")
            if name == "WebcastChatMessage":
                chat = parse_chat_message(payload)
                if chat["content"]:
                    chats.append(chat)
            elif name == "WebcastRoomUserSeqMessage":
                rank = parse_user_seq(payload)
        elif no == 5 and isinstance(val, bytes):
            internal_ext = val.decode("utf-8", "replace")
        elif no == 9 and isinstance(val, int):
            need_ack = bool(val)
    return chats, internal_ext, need_ack, rank


def parse_push_frame(buf: bytes) -> Dict[str, Any]:
    """PushFrame → {payload_type, response_bytes, log_id, seq_id, payload}。"""
    d = to_dict(buf)
    raw = d.get(8) if isinstance(d.get(8), bytes) else b""
    pt = d.get(7)
    return {
        "seq_id": d.get(1) if isinstance(d.get(1), int) else 0,
        "log_id": d.get(2) if isinstance(d.get(2), int) else 0,
        "payload_type": pt.decode("utf-8", "replace") if isinstance(pt, bytes) else "",
        "payload": _maybe_gunzip(raw),
    }


def parse_frame(buf: bytes) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """把一条 WS 二进制帧解析成 (聊天列表, 帧信息)。

    帧信息的 ``rank`` 字段携带在线/贡献榜快照（若有）。
    """
    frame = parse_push_frame(buf)
    info = {k: v for k, v in frame.items() if k != "payload"}
    payload = frame["payload"]
    if not payload:
        return [], info
    try:
        chats, internal_ext, need_ack, rank = parse_response(payload)
    except Exception:
        return [], info
    info["internal_ext"] = internal_ext
    info["need_ack"] = need_ack
    info["rank"] = rank
    return chats, info


# --------------------------------------------------------------------------- #
# 编码（只在需要手工发 ack / 心跳时才用）
# --------------------------------------------------------------------------- #

def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def tag(field_no: int, wire: int) -> bytes:
    return varint((field_no << 3) | wire)


def enc_len(field_no: int, data: bytes) -> bytes:
    return tag(field_no, WIRE_LEN) + varint(len(data)) + data


def enc_var(field_no: int, value: int) -> bytes:
    return tag(field_no, WIRE_VARINT) + varint(value)


def build_ack(log_id: int, internal_ext: str) -> bytes:
    """构造 ack 帧：PushFrame{f1=1, f2=log_id, f7="ack", f8=internal_ext}。"""
    body = enc_var(1, 1) + enc_var(2, log_id) + \
        enc_len(7, b"ack") + enc_len(8, internal_ext.encode("utf-8"))
    return body


def build_heartbeat() -> bytes:
    """心跳帧：PushFrame{f1=1, f8=gzip("")}。"""
    return enc_var(1, 1) + enc_len(8, gzip.compress(b""))


# --------------------------------------------------------------------------- #
# 自测
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    ok = True

    def check(name: str, got, want) -> None:
        nonlocal ok
        if got != want:
            ok = False
            print(f"  [FAIL] {name}: got={got!r} want={want!r}")
        else:
            print(f"  [ok]   {name}")

    # 合成一条 ChatMessage：User{f1=987654321, f3="测试用户"}, content="你好抖音"
    user = enc_var(1, 987654321) + enc_len(3, "测试用户".encode())
    chat = enc_len(2, user) + enc_len(3, "你好抖音".encode())
    message = enc_len(1, b"WebcastChatMessage") + enc_len(2, chat)
    response = enc_len(1, message)
    frame = enc_var(1, 7) + enc_len(7, b"msg") + enc_len(8, gzip.compress(response))

    chats, info = parse_frame(frame)
    check("payload_type", info["payload_type"], "msg")
    check("chat 数量", len(chats), 1)
    if chats:
        check("uid", chats[0]["uid"], "987654321")
        check("nick", chats[0]["nick"], "测试用户")
        check("content", chats[0]["content"], "你好抖音")

    # 非 gzip 的 payload 也要能解析
    frame2 = enc_var(1, 1) + enc_len(7, b"msg") + enc_len(8, response)
    chats2, _ = parse_frame(frame2)
    check("未压缩 payload", len(chats2), 1)

    # 垃圾数据不能崩
    chats3, _ = parse_frame(b"\xff\xff\xff\xff garbage")
    check("垃圾数据", chats3, [])

    # ack / 心跳能构造出来
    check("ack 长度>0", len(build_ack(123, "abc")) > 0, True)
    check("心跳长度>0", len(build_heartbeat()) > 0, True)

    # 榜消息：WebcastRoomUserSeqMessage，两个 Contributor
    def _contrib(uid, nick, rank, score):
        u = enc_var(1, uid) + enc_len(2, nick.encode())
        c = enc_var(1, score) + enc_len(2, u) + enc_var(3, rank)
        return c

    c1 = _contrib(111, "榜一大哥", 1, 9999)
    c2 = _contrib(222, "榜二", 2, 5200)
    seq = enc_len(2, c1) + enc_len(2, c2) + enc_var(3, 88) + enc_var(6, 12345)
    rank_msg = enc_len(1, b"WebcastRoomUserSeqMessage") + enc_len(2, seq)
    rank_resp = enc_len(1, rank_msg)
    rank_frame = enc_var(1, 1) + enc_len(7, b"msg") + \
        enc_len(8, gzip.compress(rank_resp))
    _, rinfo = parse_frame(rank_frame)
    r = rinfo.get("rank") or {}
    check("榜 total", r.get("total"), 88)
    check("榜 popularity", r.get("popularity"), 12345)
    check("榜 ranks 数量", len(r.get("ranks") or []), 2)
    if r.get("ranks"):
        check("榜 第一 uid", r["ranks"][0]["uid"], "111")
        check("榜 第一 nick", r["ranks"][0]["nick"], "榜一大哥")
        check("榜 第一 score", r["ranks"][0]["score"], 9999)
        check("榜 第一 rank", r["ranks"][0]["rank"], 1)

    print("  =>", "全部通过" if ok else "有失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
