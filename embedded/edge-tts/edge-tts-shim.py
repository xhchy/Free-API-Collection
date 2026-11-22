# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 XHTV
"""
edge-tts-shim —— 微软 Edge 朗读接口的 OpenAI 兼容层
====================================================

为什么用它
----------
Cloudflare 的 Deepgram Aura 只有英文音色，中文 TTS 是空缺。
微软 Edge 浏览器的「大声朗读」背后是一个公开的免费接口，支持 318 种音色、
其中 14 种中文（普通话 / 粤语 / 台湾 / 东北话），并且：

  * 完全免费，没有额度上限
  * 不需要注册、不需要 API key
  * 不需要代理，国内可直连

`edge-tts` 这个库把它的 WebSocket 协议封装好了，本层负责翻译成 OpenAI 协议。

对外接口
--------
    GET  /health
    GET  /v1/voices         列出全部音色（可按 ?lang=zh 过滤）
    POST /v1/audio/speech   OpenAI 标准 TTS

请求示例
--------
    {"model":"edge-tts","input":"你好世界","voice":"zh-CN-XiaoxiaoNeural"}
    {"model":"edge-tts","input":"Hello","voice":"nova"}          # OpenAI 名自动映射
    {"model":"edge-tts","input":"你好","voice":"zh-CN-YunxiNeural","speed":1.2}

voice 解析顺序
--------------
    1) 含 "-" 且形如 xx-XX-XxxNeural  → 直接当 Edge 音色名
    2) 属于 OpenAI 音色名（alloy/nova/…）→ 映射到对应中文音色
    3) 其它 → 按文本语言自动选：含中文用晓晓，否则用 Aria

用法
----
    python edge-tts-shim.py                 # 默认 127.0.0.1:3993
    EDGE_TTS_PORT=3994 python edge-tts-shim.py
"""

import asyncio
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import edge_tts

HOST = os.environ.get("EDGE_TTS_HOST", "127.0.0.1")
PORT = int(os.environ.get("EDGE_TTS_PORT", "3993"))

# OpenAI 音色名 → 中文音色（对中文文本友好）
VOICE_ALIAS = {
    "alloy": "zh-CN-XiaoxiaoNeural",
    "echo": "zh-CN-XiaoyiNeural",
    "fable": "zh-CN-YunxiNeural",
    "onyx": "zh-CN-YunjianNeural",
    "nova": "zh-CN-XiaoxiaoNeural",
    "shimmer": "zh-CN-XiaoyiNeural",
    "sage": "zh-CN-YunyangNeural",
    "coral": "zh-CN-XiaoyiNeural",
    "verse": "zh-CN-YunxiaNeural",
}

DEFAULT_ZH = "zh-CN-XiaoxiaoNeural"
DEFAULT_EN = "en-US-AriaNeural"

# 模型名 → 音色。让客户端「只选模型不传 voice」也能换声音。
# 键统一用小写，查表时 model 会先转小写。
MODEL_VOICE = {
    "edge-tts": None,                              # None = 按文本语言自动
    "edge-tts-xiaoxiao": "zh-CN-XiaoxiaoNeural",   # 晓晓 女 · 温柔通用
    "edge-tts-xiaoyi": "zh-CN-XiaoyiNeural",       # 晓伊 女 · 年轻
    "edge-tts-yunxi": "zh-CN-YunxiNeural",         # 云希 男 · 阳光
    "edge-tts-yunjian": "zh-CN-YunjianNeural",     # 云健 男 · 播音腔
    "edge-tts-yunyang": "zh-CN-YunyangNeural",     # 云扬 男 · 新闻播报
    "edge-tts-xiaobei": "zh-CN-liaoning-XiaobeiNeural",  # 晓北 女 · 东北话
    "edge-tts-xiaoni": "zh-CN-shaanxi-XiaoniNeural",     # 晓妮 女 · 陕西话
    "edge-tts-hk": "zh-HK-HiuMaanNeural",          # 曉曼 女 · 粤语
    "edge-tts-tw": "zh-TW-HsiaoChenNeural",        # 曉臻 女 · 台湾
}

_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_EDGE_VOICE = re.compile(r"^[a-z]{2,3}-[A-Z]{2,4}-\w+Neural$")


def _log(msg):
    sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


def pick_voice(voice, text, model=""):
    """
    四级解析，优先级从高到低：
      1) voice 本身就是标准 Edge 音色名
      2) voice 是 OpenAI 别名（alloy/nova/…）
      3) 模型名命中 MODEL_VOICE（客户端只选模型时走这里）
      4) 按文本语言自动（含中文 → 晓晓，否则 Aria）
    """
    voice = (voice or "").strip()
    if _EDGE_VOICE.match(voice):
        return voice, "explicit"
    if voice.lower() in VOICE_ALIAS:
        return VOICE_ALIAS[voice.lower()], "alias"

    mv = MODEL_VOICE.get((model or "").strip().lower())
    if mv:
        return mv, "model"

    if _CJK.search(text):
        return DEFAULT_ZH, "auto-zh"
    return DEFAULT_EN, "auto-en"


def to_rate(speed):
    """OpenAI 的 speed（0.25~4.0）转 edge-tts 的 rate 百分比字符串。"""
    try:
        s = float(speed)
    except Exception:
        return "+0%"
    s = max(0.25, min(s, 4.0))
    pct = int(round((s - 1.0) * 100))
    return "%+d%%" % pct


async def _synth(text, voice, rate, pitch, volume):
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
    buf = bytearray()
    async for chunk in comm.stream():
        if chunk.get("type") == "audio":
            buf.extend(chunk["data"])
    return bytes(buf)


def synthesize(text, voice, speed=1.0, pitch="+0Hz", volume="+0%"):
    return asyncio.run(_synth(text, voice, to_rate(speed), pitch, volume))


async def _list_voices():
    return await edge_tts.list_voices()


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _audio(self, raw):
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path, _, query = self.path.partition("?")

        if path.startswith("/health"):
            return self._json(200, {
                "status": "ok",
                "engine": "edge-tts",
                "default_zh": DEFAULT_ZH,
                "default_en": DEFAULT_EN,
                "aliases": VOICE_ALIAS,
            })

        if path.startswith("/v1/voices"):
            want = ""
            m = re.search(r"lang=([A-Za-z-]+)", query or "")
            if m:
                want = m.group(1)
            try:
                voices = asyncio.run(_list_voices())
            except Exception as e:
                return self._json(502, {"error": {"message": "list_voices failed: %s" % e}})
            if want:
                voices = [v for v in voices if v.get("Locale", "").lower().startswith(want.lower())]
            data = [{
                "id": v["ShortName"],
                "locale": v["Locale"],
                "gender": v["Gender"],
                "name": v.get("FriendlyName", ""),
            } for v in sorted(voices, key=lambda x: x["ShortName"])]
            return self._json(200, {"object": "list", "count": len(data), "data": data})

        if path.startswith("/v1/models"):
            data = [{"id": k, "object": "model", "owned_by": "microsoft-edge",
                     "voice": v or "auto"} for k, v in MODEL_VOICE.items()]
            return self._json(200, {"object": "list", "data": data})

        return self._json(404, {"error": {"message": "not found: %s" % path}})

    def do_POST(self):
        path = self.path.split("?")[0]
        if not path.startswith("/v1/audio/speech"):
            return self._json(404, {"error": {"message": "not found: %s" % path}})

        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception as e:
            return self._json(400, {"error": {"message": "bad json: %s" % e}})

        text = body.get("input") or body.get("text") or ""
        if not text.strip():
            return self._json(400, {"error": {"message": "input 为空"}})

        voice, how = pick_voice(body.get("voice"), text, body.get("model"))
        speed = body.get("speed", 1.0)
        pitch = body.get("pitch", "+0Hz")
        volume = body.get("volume", "+0%")

        t0 = time.time()
        try:
            raw = synthesize(text, voice, speed, pitch, volume)
        except Exception as e:
            _log("TTS 失败 voice=%s：%s" % (voice, e))
            return self._json(502, {"error": {"message": "%s: %s" % (type(e).__name__, e)}})

        _log("TTS ok  %-28s %5d 字  %6d 字节  %.1fs  (%s)"
             % (voice, len(text), len(raw), time.time() - t0, how))
        self._audio(raw)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    _log("edge-tts-shim 就绪   http://%s:%d" % (HOST, PORT))
    _log("中文默认 %s   英文默认 %s" % (DEFAULT_ZH, DEFAULT_EN))
    _log("端点 /health  /v1/voices  /v1/audio/speech")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        _log("收到中断，退出")


if __name__ == "__main__":
    main()
