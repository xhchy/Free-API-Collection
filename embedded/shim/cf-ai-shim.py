# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 XHTV
"""
cf-ai-shim —— Cloudflare Workers AI 兼容中间层（全能力版）
==========================================================

为什么需要它
------------
Cloudflare 的 OpenAI 兼容端点（/ai/v1/chat/completions）对视觉模型有几处不合规：

  1. 图片必须 base64（传 URL 直接 400 code 6004）
  2. 图片不能放在 content 数组里（标准 OpenAI 写法会 400 code 3030），
     它要的是 message 顶层挂一个 image_url
  3. 首次调用视觉模型必须先提交一次 {"prompt":"agree"}

而视觉之外的音频 / 图像生成 / 向量 / 重排，Cloudflare 只提供原生 /ai/run 端点，
返回结构完全不是 OpenAI 格式，NewAPI 无法直接对接。本层负责把这些补齐。

本中间层做的事
--------------
  视觉理解  POST /v1/chat/completions
      URL 图下载转 base64 → 图片重排到文字前 → 打原生 /ai/run
      → 遇 5016 自动 agree 重试 → {"result":{...}} 转 OpenAI chat.completion

  语音识别  POST /v1/audio/transcriptions   (multipart 或 JSON base64)
      → @cf/openai/whisper（该模型只吃字节数组，本层负责转换）
      → {"text": ...} / verbose_json

  语音合成  POST /v1/audio/speech
      → @cf/deepgram/aura-*  返回 audio/mpeg 二进制
      OpenAI 的 voice 名（alloy/nova/…）自动映射到 Deepgram 音色

  图像生成  POST /v1/images/generations
      → @cf/black-forest-labs/flux-1-schnell  {"result":{"image": "<b64>"}}
      → OpenAI 的 {"created":..., "data":[{"b64_json":...}]}

  文本向量  POST /v1/embeddings
      → @cf/baai/bge-* / qwen3-embedding  {"result":{"data":[[...]]}}
      → OpenAI 的 {"object":"list","data":[{index,embedding}],"usage":{...}}

  文档重排  POST /v1/rerank   （自定义端点，非 OpenAI 标准）
      → @cf/baai/bge-reranker-base，contexts 必须是 [{"text":...}]

  其余模型  POST /v1/chat/completions → 原样转发兼容端点，不掺和

用法
----
    python cf-ai-shim.py                 # 默认 127.0.0.1:3025
    CF_SHIM_PORT=3998 python cf-ai-shim.py

环境变量
--------
    CF_ACCOUNT_ID   账号 ID（默认已填）
    CF_API_TOKEN    cfut_ 开头的用户级令牌（默认已填）
    CF_SHIM_PORT    监听端口，默认 3025
    CF_SHIM_HOST    监听地址，默认 127.0.0.1
    CF_SHIM_PROXY   下不动图时用的代理，默认 http://127.0.0.1:7897

对外接口
--------
    GET  /health
    GET  /v1/models
    POST /v1/chat/completions
    POST /v1/audio/transcriptions
    POST /v1/audio/speech
    POST /v1/images/generations
    POST /v1/embeddings
    POST /v1/rerank
"""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 配置

ACCOUNT_ID = ""
API_TOKEN = ""
HOST = os.environ.get("CF_SHIM_HOST", "127.0.0.1")
PORT = int(os.environ.get("CF_SHIM_PORT", "3025"))
PROXY = os.environ.get("CF_SHIM_PROXY", "http://127.0.0.1:7897")

# ---- 凭据来源：环境变量 → vault/keys.json 的 cloudflare 段 → 报错退出
# 2026-11-22：原版把 CF 账号 ID 和 cfut_ 令牌写死在本文件里，而那套代码是要发布到
# GitHub 的免费 API 合集 ⇒ 等于把主人的 Cloudflare 账号公开。改为运行时注入。
HERE = os.path.dirname(os.path.abspath(__file__))
VAULT_KEYS = os.environ.get(
    "VAULT_KEYS", os.path.join(HERE, "..", "..", "vault", "keys.json")
)


def _from_vault(field):
    """从 vault/keys.json 的 cloudflare 段取凭据；文件不存在或解析失败就返回空串。"""
    try:
        with open(VAULT_KEYS, "r", encoding="utf-8") as f:
            cf = json.load(f).get("cloudflare") or {}
        return str(cf.get(field) or "").strip()
    except (OSError, ValueError):
        return ""


ACCOUNT_ID = (os.environ.get("CF_ACCOUNT_ID") or _from_vault("account_id")).strip()
API_TOKEN = (os.environ.get("CF_API_TOKEN") or _from_vault("api_token")).strip()

if not ACCOUNT_ID or not API_TOKEN:
    sys.stderr.write(
        "[cf-ai-shim] 缺少凭据：请设置环境变量 CF_ACCOUNT_ID / CF_API_TOKEN，\n"
        "             或在 %s 里填 cloudflare.account_id / cloudflare.api_token\n"
        "             （模板见 vault/keys.example.json）\n" % os.path.normpath(VAULT_KEYS)
    )
    sys.exit(2)

API_ROOT = "https://api.cloudflare.com/client/v4/accounts/%s/ai" % ACCOUNT_ID
NATIVE_URL = API_ROOT + "/run/%s"
COMPAT_URL = API_ROOT + "/v1/chat/completions"

MAX_IMAGE_BYTES = 12 * 1024 * 1024      # 单张图上限
MAX_AUDIO_BYTES = 25 * 1024 * 1024      # 单段音频上限
IMAGE_TIMEOUT = 30                      # 下载超时
CALL_TIMEOUT = 180                      # 上游调用超时

# ---- 按能力分组的模型白名单（决定走哪个处理分支）

VISION_MODELS = {
    "@cf/meta/llama-3.2-11b-vision-instruct",
    "@cf/moondream/moondream3.1-9B-A2B",
}

ASR_MODELS = {
    "@cf/openai/whisper",
}

TTS_MODELS = {
    "@cf/deepgram/aura-1",
    "@cf/deepgram/aura-2-en",
    "@cf/deepgram/aura-2-es",
}

IMAGE_GEN_MODELS = {
    "@cf/black-forest-labs/flux-1-schnell",
}

EMBED_MODELS = {
    "@cf/baai/bge-small-en-v1.5",
    "@cf/baai/bge-base-en-v1.5",
    "@cf/baai/bge-large-en-v1.5",
    "@cf/baai/bge-m3",
    "@cf/qwen/qwen3-embedding-0.6b",
}

RERANK_MODELS = {
    "@cf/baai/bge-reranker-base",
}

# OpenAI 音色名 → Deepgram 音色名
VOICE_MAP = {
    "alloy": "angus",
    "echo": "asteria",
    "fable": "orion",
    "onyx": "arcas",
    "nova": "luna",
    "shimmer": "stella",
    "sage": "helios",
    "coral": "athena",
    "verse": "perseus",
}

ALL_MODELS = sorted(VISION_MODELS | ASR_MODELS | TTS_MODELS | IMAGE_GEN_MODELS
                    | EMBED_MODELS | RERANK_MODELS)

# 已替用户同意过协议的模型，避免每次请求都试一遍
_agreed = set()

# 不走系统代理：本机有 13328 / 7897 两个代理变量，直连 Cloudflare 没问题
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _log(msg):
    sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


# ---------------------------------------------------------------- HTTP 工具

def _post_json(url, payload, timeout=CALL_TIMEOUT, opener=_NO_PROXY):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", "Bearer " + API_TOKEN)
    req.add_header("Content-Type", "application/json")
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_raw(url, payload, timeout=CALL_TIMEOUT, opener=_NO_PROXY):
    """返回 (原始字节, content-type)。用于 TTS 这类吐二进制的端点。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", "Bearer " + API_TOKEN)
    req.add_header("Content-Type", "application/json")
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(), (resp.headers.get("Content-Type") or "application/octet-stream")


def _err_text(e, limit=300):
    """把 HTTPError 的响应体裁成可读字符串。"""
    try:
        return e.read().decode("utf-8", "ignore")[:limit]
    except Exception:
        return str(e)[:limit]


def _guess_mime(raw, hint_url=""):
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    m = re.search(r"\.(png|jpe?g|gif|webp|bmp)(?:\?|$)", hint_url, re.I)
    if m:
        ext = m.group(1).lower()
        return {"jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/" + ext)
    return "image/jpeg"


def _fetch_as_data_uri(url):
    """把 http(s) 图片抓下来，返回 data:image/...;base64,... ；失败返回 None。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cf-ai-shim"})
        with _NO_PROXY.open(req, timeout=IMAGE_TIMEOUT) as resp:
            raw = resp.read(MAX_IMAGE_BYTES + 1)
    except Exception as e:
        _log("直连下载失败(%s)，改走代理：%s" % (e.__class__.__name__, url[:70]))
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cf-ai-shim"})
            with opener.open(req, timeout=IMAGE_TIMEOUT) as resp:
                raw = resp.read(MAX_IMAGE_BYTES + 1)
        except Exception as e2:
            _log("代理下载也失败：%s" % e2)
            return None
    if len(raw) > MAX_IMAGE_BYTES:
        _log("图片超过 %d 字节，丢弃" % MAX_IMAGE_BYTES)
        return None
    return "data:%s;base64,%s" % (_guess_mime(raw, url), base64.b64encode(raw).decode())


# ---------------------------------------------------------------- multipart 解析

def parse_multipart(body, content_type):
    """
    极简 multipart/form-data 解析，只取表单字段与文件内容。
    返回 {name: {"filename": str|None, "data": bytes, "content_type": str|None}}
    Python 3.13 起 cgi 模块已移除，所以手写。
    """
    m = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not m:
        return {}
    boundary = ("--" + m.group(1)).encode("utf-8")
    out = {}
    for chunk in body.split(boundary):
        if not chunk or chunk in (b"--\r\n", b"--", b"\r\n"):
            continue
        chunk = chunk.lstrip(b"\r\n")
        if b"\r\n\r\n" not in chunk:
            continue
        head, _, data = chunk.partition(b"\r\n\r\n")
        head_txt = head.decode("utf-8", "ignore")
        nm = re.search(r'name="([^"]*)"', head_txt)
        if not nm:
            continue
        fn = re.search(r'filename="([^"]*)"', head_txt)
        ct = re.search(r"Content-Type:\s*([^\r\n]+)", head_txt, re.I)
        if data.endswith(b"\r\n"):
            data = data[:-2]
        out[nm.group(1)] = {
            "filename": fn.group(1) if fn else None,
            "data": data,
            "content_type": ct.group(1).strip() if ct else None,
        }
    return out


# ---------------------------------------------------------------- 视觉：请求改写

def _normalize_image_part(part):
    """把一个图片 part 统一成 {type:image_url, image_url:{url:<data-uri>}}，失败返回 None。"""
    url = None
    if isinstance(part, dict):
        iu = part.get("image_url")
        if isinstance(iu, dict):
            url = iu.get("url")
        elif isinstance(iu, str):
            url = iu
        elif isinstance(part.get("url"), str):
            url = part["url"]
    if not url:
        return None
    if url.startswith("data:"):
        return {"type": "image_url", "image_url": {"url": url}}
    if url.startswith(("http://", "https://")):
        data_uri = _fetch_as_data_uri(url)
        if not data_uri:
            return None
        return {"type": "image_url", "image_url": {"url": data_uri}}
    return None


def _prepare_messages(messages):
    """
    把标准 OpenAI messages 改写成 Cloudflare 原生端点能吃的形态：
      - 图片统一转 data-uri
      - 图片排在文字前面
      - 支持 message 顶层挂 image_url 的老式写法
    """
    out = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        content = msg.get("content")

        images, texts = [], []

        if msg.get("image_url"):
            p = _normalize_image_part({"image_url": msg["image_url"]})
            if p:
                images.append(p)

        if isinstance(content, str):
            if content.strip():
                texts.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype in ("image_url", "image", "input_image"):
                    p = _normalize_image_part(part)
                    if p:
                        images.append(p)
                elif ptype in ("text", "input_text"):
                    txt = part.get("text") or ""
                    if txt.strip():
                        texts.append({"type": "text", "text": txt})
                elif isinstance(part.get("text"), str) and part["text"].strip():
                    texts.append({"type": "text", "text": part["text"]})

        merged = images + texts
        if merged:
            out.append({"role": role, "content": merged})
        else:
            out.append({"role": role, "content": ""})
    return out


def _agree(model):
    """替用户同意 Meta 社区协议（视觉模型首次调用必做）。"""
    if model in _agreed:
        return True
    try:
        _post_json(NATIVE_URL % model, {"prompt": "agree"}, timeout=60)
        _agreed.add(model)
        _log("已提交 agree：%s" % model)
        return True
    except urllib.error.HTTPError as e:
        body = _err_text(e, 400)
        if "Thank you for agreeing" in body or "5016" in body:
            _agreed.add(model)
            _log("协议此前已同意：%s" % model)
            return True
        _log("agree 失败：HTTP %s %s" % (e.code, body[:160]))
        return False
    except Exception as e:
        _log("agree 异常：%s" % e)
        return False


def _needs_agree(err_text):
    return ("Model Agreement" in err_text) or ("5016" in err_text)


# ---------------------------------------------------------------- 业务：视觉

def handle_vision(body):
    """视觉模型：原生端点 + 结果转 OpenAI 格式。"""
    model = body.get("model")
    messages = _prepare_messages(body.get("messages"))
    payload = {"messages": messages}
    for k in ("max_tokens", "temperature", "top_p", "seed", "frequency_penalty"):
        if body.get(k) is not None:
            payload[k] = body[k]

    try:
        data = _post_json(NATIVE_URL % model, payload)
    except urllib.error.HTTPError as e:
        err = _err_text(e)
        if e.code in (403, 400) and _needs_agree(err) and _agree(model):
            data = _post_json(NATIVE_URL % model, payload)
        else:
            raise RuntimeError("上游 HTTP %s：%s" % (e.code, err))

    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("上游返回格式异常：%s" % json.dumps(data, ensure_ascii=False)[:300])

    text = result.get("response") or ""
    if not text and result.get("choices"):
        text = result["choices"][0].get("message", {}).get("content", "")
    usage = result.get("usage") or {}
    return {
        "id": "chatcmpl-cf-" + str(int(time.time() * 1000)),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


def handle_text(body):
    """非视觉模型：原样转发兼容端点。"""
    return _post_json(COMPAT_URL, body)


# ---------------------------------------------------------------- 业务：语音识别

def handle_transcriptions(raw_body, content_type, body_json=None):
    """
    支持两种入参：
      A) multipart/form-data，带 file 字段（OpenAI 标准写法，NewAPI 转发就是这个）
      B) application/json，{"file": "<base64 或 data-uri>", "model": "..."}
    Cloudflare 的 whisper 只接受字节数组，这里负责转换。
    """
    model = None
    audio = None

    if body_json is not None:
        model = body_json.get("model")
        f = body_json.get("file") or body_json.get("audio")
        if isinstance(f, str):
            if f.startswith("data:"):
                f = f.split(",", 1)[-1]
            try:
                audio = base64.b64decode(f)
            except Exception as e:
                raise RuntimeError("file 字段不是合法 base64：%s" % e)
    else:
        fields = parse_multipart(raw_body, content_type)
        model = (fields.get("model", {}).get("data") or b"").decode("utf-8", "ignore") or None
        fld = fields.get("file") or fields.get("audio")
        if fld:
            audio = fld["data"]

    if not audio:
        raise RuntimeError("没有收到音频数据（file / audio 字段为空）")
    if len(audio) > MAX_AUDIO_BYTES:
        raise RuntimeError("音频超过 %d 字节" % MAX_AUDIO_BYTES)

    model = model or "@cf/openai/whisper"
    if model not in ASR_MODELS:
        _log("ASR 模型 %s 不在白名单，仍尝试转发" % model)

    # whisper 吃字节数组；转 list 后 JSON 体积约为原始 3-4 倍，25MB 上限可控
    payload = {"audio": list(audio)}

    data = _post_json(NATIVE_URL % model, payload, timeout=300)
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("上游返回格式异常：%s" % json.dumps(data, ensure_ascii=False)[:300])

    text = result.get("text") or ""
    return {
        "text": text,
        "task": "transcribe",
        "language": result.get("language") or "unknown",
        "duration": result.get("duration"),
        "words": result.get("words"),
        "vtt": result.get("vtt"),
        "word_count": result.get("word_count"),
        "model": model,
    }


# ---------------------------------------------------------------- 业务：语音合成

def handle_speech(body):
    """返回 (音频字节, content-type)。"""
    model = body.get("model") or "@cf/deepgram/aura-1"
    text = body.get("input") or body.get("text") or ""
    if not text.strip():
        raise RuntimeError("input 为空")

    voice = body.get("voice") or "angus"
    voice = VOICE_MAP.get(str(voice).lower(), voice)

    payload = {"text": text}
    # 只有 Deepgram 系接受 voice 参数
    if "deepgram" in model:
        payload["voice"] = voice

    raw, ctype = _post_raw(NATIVE_URL % model, payload, timeout=300)
    if ctype.startswith("application/json"):
        # 出错时上游会返回 JSON
        raise RuntimeError("上游返回错误：%s" % raw.decode("utf-8", "ignore")[:300])
    return raw, ctype


# ---------------------------------------------------------------- 业务：图像生成

def handle_images(body):
    model = body.get("model") or "@cf/black-forest-labs/flux-1-schnell"
    prompt = body.get("prompt") or ""
    if not prompt.strip():
        raise RuntimeError("prompt 为空")

    n = int(body.get("n") or 1)
    n = max(1, min(n, 4))               # 免费额度有限，单次最多 4 张
    steps = body.get("steps")
    if steps is None:
        steps = 4
    steps = max(1, min(int(steps), 8))  # flux-1-schnell 只支持 1-8

    data_items = []
    for _ in range(n):
        data = _post_json(NATIVE_URL % model, {"prompt": prompt, "steps": steps}, timeout=300)
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict) or not result.get("image"):
            raise RuntimeError("上游返回格式异常：%s" % json.dumps(data, ensure_ascii=False)[:300])
        data_items.append({"b64_json": result["image"], "revised_prompt": prompt})

    return {
        "created": int(time.time()),
        "data": data_items,
    }


# ---------------------------------------------------------------- 业务：向量

def handle_embeddings(body):
    model = body.get("model") or "@cf/baai/bge-small-en-v1.5"
    inp = body.get("input")
    if inp is None:
        raise RuntimeError("input 为空")
    texts = [inp] if isinstance(inp, str) else list(inp)
    if not texts:
        raise RuntimeError("input 为空数组")

    data = _post_json(NATIVE_URL % model, {"text": texts}, timeout=300)
    result = data.get("result") if isinstance(data, dict) else None
    vectors = (result or {}).get("data")
    if not isinstance(vectors, list):
        raise RuntimeError("上游返回格式异常：%s" % json.dumps(data, ensure_ascii=False)[:300])

    items = [{
        "object": "embedding",
        "index": i,
        "embedding": v,
    } for i, v in enumerate(vectors)]

    # 粗算 token：CF 不返回精确值，按字符数 / 4 估
    approx = sum(max(1, len(t) // 4) for t in texts)
    return {
        "object": "list",
        "data": items,
        "model": model,
        "usage": {"prompt_tokens": approx, "total_tokens": approx},
    }


# ---------------------------------------------------------------- 业务：重排

def handle_rerank(body):
    """
    非 OpenAI 标准端点，形如：
      {"model":"@cf/baai/bge-reranker-base","query":"...","documents":["a","b"],"top_n":2}
    返回：
      {"results":[{"index":0,"relevance_score":0.93}, ...]}
    """
    model = body.get("model") or "@cf/baai/bge-reranker-base"
    query = body.get("query") or ""
    docs = body.get("documents") or body.get("contexts") or []
    if isinstance(docs, str):
        docs = [docs]
    if not query or not docs:
        raise RuntimeError("query / documents 不能为空")

    contexts = [{"text": d if isinstance(d, str) else (d.get("text") or "")} for d in docs]
    data = _post_json(NATIVE_URL % model, {"query": query, "contexts": contexts}, timeout=300)
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("上游返回格式异常：%s" % json.dumps(data, ensure_ascii=False)[:300])

    raw = result.get("response") or result.get("data") or []
    items = []
    for i, item in enumerate(raw):
        score = item.get("score") if isinstance(item, dict) else item
        items.append({"index": i, "relevance_score": float(score or 0)})
    items.sort(key=lambda x: x["relevance_score"], reverse=True)

    top_n = body.get("top_n")
    if top_n:
        items = items[: int(top_n)]
    for rank, it in enumerate(items):
        it["index"] = it.pop("index")
        it["document_index"] = it["index"]
        it["rank"] = rank
    return {"results": items, "model": model}


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _send_json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_binary(self, code, raw, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_sse(self, payload):
        cid = payload["id"]
        content = payload["choices"][0]["message"]["content"]
        created = payload["created"]
        model = payload["model"]

        def chunk(delta, finish=None):
            obj = {
                "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(chunk({"role": "assistant", "content": content}))
        self.wfile.write(chunk({}, "stop"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True

    # ---------------- GET

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send_json(200, {
                "status": "ok",
                "account": ACCOUNT_ID[:8] + "…",
                "counts": {
                    "vision": len(VISION_MODELS), "asr": len(ASR_MODELS),
                    "tts": len(TTS_MODELS), "image": len(IMAGE_GEN_MODELS),
                    "embed": len(EMBED_MODELS), "rerank": len(RERANK_MODELS),
                },
                "models": ALL_MODELS,
            })
        if self.path.startswith("/v1/models"):
            data = [{"id": m, "object": "model", "owned_by": "cloudflare"} for m in ALL_MODELS]
            return self._send_json(200, {"object": "list", "data": data})
        return self._send_json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    # ---------------- POST

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
        except Exception as e:
            return self._send_json(400, {"error": {"message": "read body failed: %s" % e}})

        ctype = (self.headers.get("Content-Type") or "").lower()
        is_json = ctype.startswith("application/json")

        # ---- 语音识别（multipart 或 json）
        if path.startswith("/v1/audio/transcriptions"):
            try:
                body_json = None
                if is_json and raw:
                    body_json = json.loads(raw.decode("utf-8"))
                out = handle_transcriptions(raw, ctype, body_json)
                return self._send_json(200, out)
            except Exception as e:
                _log("ASR 失败：%s" % e)
                return self._send_json(502, {"error": {"message": str(e), "type": "upstream_error"}})

        # ---- 其余全部按 JSON 解析
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as e:
            return self._send_json(400, {"error": {"message": "bad json: %s" % e, "type": "invalid_request_error"}})

        model = body.get("model") or ""

        # ---- 语音合成
        if path.startswith("/v1/audio/speech"):
            try:
                audio, ctype_out = handle_speech(body)
                return self._send_binary(200, audio, ctype_out)
            except Exception as e:
                _log("TTS 失败 model=%s：%s" % (model, e))
                return self._send_json(502, {"error": {"message": str(e), "type": "upstream_error"}})

        # ---- 图像生成
        if path.startswith("/v1/images/generations"):
            try:
                return self._send_json(200, handle_images(body))
            except Exception as e:
                _log("图像生成失败 model=%s：%s" % (model, e))
                return self._send_json(502, {"error": {"message": str(e), "type": "upstream_error"}})

        # ---- 向量
        if path.startswith("/v1/embeddings"):
            try:
                return self._send_json(200, handle_embeddings(body))
            except Exception as e:
                _log("Embedding 失败 model=%s：%s" % (model, e))
                return self._send_json(502, {"error": {"message": str(e), "type": "upstream_error"}})

        # ---- 重排
        if path.startswith("/v1/rerank"):
            try:
                return self._send_json(200, handle_rerank(body))
            except Exception as e:
                _log("Rerank 失败 model=%s：%s" % (model, e))
                return self._send_json(502, {"error": {"message": str(e), "type": "upstream_error"}})

        # ---- 对话
        if not path.startswith("/v1/chat/completions"):
            return self._send_json(404, {"error": {"message": "not found: %s" % path, "type": "invalid_request_error"}})

        want_stream = bool(body.get("stream"))
        is_vision = model in VISION_MODELS or "vision" in model

        try:
            payload = handle_vision(body) if is_vision else handle_text(body)
        except Exception as e:
            _log("请求失败 model=%s：%s" % (model, e))
            return self._send_json(502, {"error": {"message": str(e), "type": "upstream_error"}})

        if want_stream:
            return self._send_sse(payload)
        return self._send_json(200, payload)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    _log("cf-ai-shim 就绪   http://%s:%d" % (HOST, PORT))
    _log("账号 %s" % ACCOUNT_ID)
    _log("能力：视觉 %d / ASR %d / TTS %d / 图像 %d / 向量 %d / 重排 %d"
         % (len(VISION_MODELS), len(ASR_MODELS), len(TTS_MODELS),
            len(IMAGE_GEN_MODELS), len(EMBED_MODELS), len(RERANK_MODELS)))
    _log("端点：/v1/chat/completions  /v1/audio/transcriptions  /v1/audio/speech")
    _log("      /v1/images/generations  /v1/embeddings  /v1/rerank  /health  /v1/models")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        _log("收到中断，退出")


if __name__ == "__main__":
    main()
