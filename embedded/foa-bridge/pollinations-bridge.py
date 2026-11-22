# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 XHTV
"""Minimal OpenAI-compatible bridge in front of https://text.pollinations.ai

Why: free-one-api 的 ds-webagent/deepseek-web 适配器要求上游同时提供
GET /health（返回 {"ready": true}）和 POST /v1/chat/completions。
text.pollinations.ai 只有后者（/health 会被当成聊天问题回答），
所以在本机起一个 40 行的转发层，把两者拼在一起。

Listen: 127.0.0.1:3061 (内部用，不对外)
Upstream: https://text.pollinations.ai
"""

import json
import os

from aiohttp import ClientSession, ClientTimeout, web

UPSTREAM = os.environ.get("POLLINATIONS_UPSTREAM", "https://text.pollinations.ai").rstrip("/")
LISTEN_HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("BRIDGE_PORT", "3061"))
DEFAULT_MODEL = os.environ.get("BRIDGE_MODEL", "openai")
TIMEOUT = ClientTimeout(total=180)


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ready": True, "status": "ok", "upstream": UPSTREAM})


async def models(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "object": "list",
            "data": [
                {"id": DEFAULT_MODEL, "object": "model", "created": 0, "owned_by": "pollinations"},
            ],
        }
    )


async def chat_completions(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.json_response(
            {"error": {"message": "invalid json body", "type": "invalid_request_error"}}, status=400
        )

    # 对齐模型名：上游只认 openai / openai-fast 等，其它名字落到默认模型。
    if not payload.get("model"):
        payload["model"] = DEFAULT_MODEL

    async with ClientSession(timeout=TIMEOUT) as session:
        async with session.post(UPSTREAM + "/v1/chat/completions", json=payload) as resp:
            body = await resp.read()
            if resp.status != 200:
                return web.Response(
                    body=body,
                    status=resp.status,
                    content_type=resp.content_type or "application/json",
                )
            if payload.get("stream"):
                return web.Response(
                    body=body,
                    status=200,
                    content_type="text/event-stream",
                    headers={"Cache-Control": "no-cache"},
                )
            return web.Response(body=body, status=200, content_type="application/json")


app = web.Application()
app.router.add_get("/health", health)
app.router.add_get("/v1/models", models)
app.router.add_post("/v1/chat/completions", chat_completions)


if __name__ == "__main__":
    print("pollinations bridge on http://%s:%d -> %s" % (LISTEN_HOST, LISTEN_PORT, UPSTREAM), flush=True)
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=None)
