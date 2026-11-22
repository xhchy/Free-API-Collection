# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 XHTV
"""
keyvault —— 新NEWAPI(9978) 的凭证保管 + 转发层
==============================================

解决的问题
----------
NewAPI 后台会把每个渠道的上游 APIKEY 明文展示给管理员（包括超管），
一旦给了别人后台权限，等于把所有上游凭证交出去。

本层把上游凭证从数据库里彻底剥离：
  * 真实 key 只存在 new-api-new/vault/keys.json（文件权限 600，不进程间共享）
  * 数据库里每条渠道的 key 都是占位串 "vault-only"，谁看都一样
  * 渠道的 base_url 改成 http://127.0.0.1:3997/relay/<route>
  * 本层收到请求后剥掉 /relay/<route> 前缀，注入真实 key，转发给真正的上游

路由表（vault/keys.json）
------------------------
{
  "routes": {
    "grok": {"upstream":"http://127.0.0.1:3999", "key":"...", "strip_prefix":"", "proxy":false},
    ...
  }
}

支持：
  * GET/POST/PUT/DELETE 全方法透传
  * 流式透传（SSE 逐块写出，不缓冲）
  * 单路由代理（gemini 需要过 7897）
  * strip_prefix（例如 Gemini 的 OpenAI 兼容端点在 /v1beta/openai，
    而 NewAPI 会再拼 /v1/chat/completions，需要剥掉这个 /v1）

用法
----
    python vault.py               # 默认 127.0.0.1:3997
    VAULT_PORT=3997 python vault.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("VAULT_HOST", "127.0.0.1")
PORT = int(os.environ.get("VAULT_PORT", "3997"))
HERE = os.path.dirname(os.path.abspath(__file__))
KEYS_FILE = os.environ.get("VAULT_KEYS", os.path.join(HERE, "keys.json"))
PREFIX = "/relay/"

_routes = {}
_opener_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_openers = {}


def log(msg):
    sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


def load_routes():
    global _routes
    with open(KEYS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    _routes = data.get("routes", {})
    log("已加载 %d 条路由：%s" % (len(_routes), ", ".join(sorted(_routes))))
    for name, r in _routes.items():
        if r.get("proxy"):
            proxy = os.environ.get("VAULT_PROXY", "http://127.0.0.1:7897")
            _openers[name] = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        else:
            _openers[name] = _opener_direct


def opener_for(name):
    return _openers.get(name, _opener_direct)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    # ---------------- 统一转发 ----------------
    def _relay(self, method):
        path = self.path
        if not path.startswith(PREFIX):
            return self._json(404, {"error": {"message": "unknown path, use /relay/<route>/..."}})

        rest = path[len(PREFIX):]
        route_name, _, tail = rest.partition("/")
        route = _routes.get(route_name)
        if not route:
            return self._json(404, {"error": {"message": "unknown route: %s" % route_name}})

        upstream = route["upstream"].rstrip("/")
        sub_path = "/" + tail if tail else "/"

        strip = route.get("strip_prefix") or ""
        if strip and sub_path.startswith(strip + "/"):
            sub_path = sub_path[len(strip):]
        elif strip and sub_path == strip:
            sub_path = "/"

        url = upstream + sub_path
        if self.path.count("?"):                       # 保留查询串
            url += "?" + self.path.split("?", 1)[1]

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        req = urllib.request.Request(url, data=body, method=method)
        # 头部透传（去掉 hop-by-hop 与客户端认证）
        for h in ("Content-Type", "Accept", "Accept-Language", "User-Agent"):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)
        real_key = route.get("key") or ""
        if real_key:
            req.add_header("Authorization", "Bearer " + real_key)

        try:
            resp = opener_for(route_name).open(req, timeout=300)
        except urllib.error.HTTPError as e:
            detail = e.read()[:4000]
            self.send_response(e.code)
            ctype = e.headers.get("Content-Type") or "application/json"
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(detail)))
            self.end_headers()
            self.wfile.write(detail)
            log("%s %s → 上游 %s（已回传错误体）" % (method, sub_path, e.code))
            return
        except Exception as e:
            return self._json(502, {"error": {"message": "vault upstream error: %s" % e}})

        # 流式回写
        self.send_response(resp.status)
        ctype = resp.headers.get("Content-Type") or "application/json"
        self.send_header("Content-Type", ctype)
        if resp.headers.get("Cache-Control"):
            self.send_header("Cache-Control", resp.headers["Cache-Control"])
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception as e:
            log("回写中断：%s" % e)

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", ""):
            return self._json(200, {"status": "ok", "routes": sorted(_routes)})
        return self._relay("GET")

    def do_POST(self):
        return self._relay("POST")

    def do_PUT(self):
        return self._relay("PUT")

    def do_DELETE(self):
        return self._relay("DELETE")

    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main():
    load_routes()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log("keyvault 就绪  http://%s:%d   （真实 key 只在 %s）" % (HOST, PORT, KEYS_FILE))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("退出")


if __name__ == "__main__":
    main()
