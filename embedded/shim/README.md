# cf-ai-shim —— Cloudflare Workers AI 全能力兼容层

单文件、纯标准库、无依赖。把 Cloudflare Workers AI 的原生端点翻译成 OpenAI 协议，
让 NewAPI 这类中转站能直接对接视觉、语音、图像、向量、重排五类能力。

```
监听 127.0.0.1:3998（内置版） / 3025（旧版）
账号 5bb965a6…  免费额度 10000 Neurons/天
```

## 为什么需要它

Cloudflare 只对「文本对话」提供了 OpenAI 兼容端点（`/ai/v1/chat/completions`），
而且连视觉都有三处不合规：

| 问题 | 现象 |
|---|---|
| 图片必须是 base64 | 传 URL → 400 `6004` |
| 图片不能放 content 数组 | 标准写法 → 400 `3030`，它要 message 顶层 `image_url` |
| 首次调用视觉要先同意协议 | 403 `5016`，需先提交 `{"prompt":"agree"}` |

视觉之外（语音识别/合成、图像生成、向量、重排）**只有原生 `/ai/run/{model}`**，
返回结构是 `{"result":{...}}`，不是 OpenAI 格式。

本层把这些全部抹平。

## 对外接口

| 方法 | 路径 | 上游 | 说明 |
|---|---|---|---|
| GET | `/health` | — | 返回各能力模型计数 |
| GET | `/v1/models` | — | 13 个模型的 OpenAI 格式清单 |
| POST | `/v1/chat/completions` | 原生 `/ai/run`（视觉）或兼容端点（文本） | 图片 URL 自动转 base64、图片重排到文字前、自动 agree |
| POST | `/v1/audio/transcriptions` | `@cf/openai/whisper` | 吃 multipart 或 JSON base64，转字节数组 |
| POST | `/v1/audio/speech` | `@cf/deepgram/aura-*` | 返回 `audio/mpeg` 二进制 |
| POST | `/v1/images/generations` | `@cf/black-forest-labs/flux-1-schnell` | 返回 `b64_json` |
| POST | `/v1/embeddings` | `@cf/baai/bge-*` | 返回 OpenAI `{object:list,data:[...]}` |
| POST | `/v1/rerank` | `@cf/baai/bge-reranker-base` | 自定义端点，非 OpenAI 标准 |

## 支持的模型（13 个）

```
视觉理解   @cf/meta/llama-3.2-11b-vision-instruct
          @cf/moondream/moondream3.1-9B-A2B
语音识别   @cf/openai/whisper
语音合成   @cf/deepgram/aura-1  aura-2-en  aura-2-es
图像生成   @cf/black-forest-labs/flux-1-schnell
文本向量   @cf/baai/bge-small-en-v1.5  bge-base-en-v1.5  bge-large-en-v1.5  bge-m3
          @cf/qwen/qwen3-embedding-0.6b
文档重排   @cf/baai/bge-reranker-base
```

## 几个容易踩的坑（都是实测结论）

1. **whisper 不吃 base64 字符串**，只吃字节数组 `{"audio":[1,2,3...]}`。
   传 base64 会报 `Type mismatch of '/audio', 'array' not in 'string'`。
   本层自动把 multipart 的文件内容转成整数列表。

2. **`whisper-large-v3-turbo` 反过来只吃 string / binary**，与 whisper 相反。
   目前没启用它，要启记得改格式。

3. **flux 的 `steps` 只能是 1–8**，默认 4。超出范围上游直接拒。

4. **reranker 的 `contexts` 必须是 `[{"text":"..."}]`**，
   传纯字符串数组会报 `contexts[0] must be an object with a 'text' field`。

5. **TTS 的 voice 名两套体系**：OpenAI 是 `alloy/nova/onyx/...`，
   Deepgram 是 `angus/luna/arcas/...`。本层内置映射表，客户按 OpenAI 习惯传即可。

6. **Deepgram 不支持中文**。中文 TTS 现在没有可用上游，`aura-*` 只会读英文。

7. **`gemma-4-26b-a4b-it` 是 reasoning 模型**，`max_tokens` 给小了会只消耗在思考上，
   返回空 content。至少给 100。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `CF_ACCOUNT_ID` | 已填 | Cloudflare 账号 ID |
| `CF_API_TOKEN` | 已填 | `cfut_` 开头的用户级令牌 |
| `CF_SHIM_PORT` | `3025` | 监听端口（内置版用 3998） |
| `CF_SHIM_HOST` | `127.0.0.1` | 监听地址 |
| `CF_SHIM_PROXY` | `http://127.0.0.1:7897` | 下载外链图片失败时的兜底代理 |

## 启动

```bat
REM 内置版（3998）
new-api-new\embedded\run-shim.bat

REM 手动
set CF_SHIM_PORT=3998
python cf-ai-shim.py
```

## 实测记录（2026-11-21）

```
GET  /health                    → vision:2 asr:1 tts:3 image:1 embed:5 rerank:1
POST /v1/audio/speech           → 13949 字节 audio/mpeg
POST /v1/audio/transcriptions   → "Hello, this is the extended Chim speaking." + 7 个 word 时间戳
POST /v1/images/generations     → 1024×1024 红苹果 JPEG，b64 511912 字符
POST /v1/embeddings             → 2 条 × 384 维
POST /v1/rerank                 → 相关 0.994 / 无关 0.00004
POST /v1/chat/completions(图)   → "这是一只红熊猫"
```

以上全部经由 9978 中转站再转发一次，验证过 NewAPI 的透传能力。

## 已知限制

- TTS **只支持英文**（Deepgram Aura 系列无中文音色）
- flux-1-schnell **不接受尺寸参数**，固定 1024×1024
- 向量维度取决于模型：bge-small 384 / bge-base 768 / bge-large 1024 / bge-m3 1024
- 单次 TTS 文本过长会失败，Aura 单次上限约 2000 字符
- 免费计划 10000 Neurons/天，图像生成一张约 30-50 Neurons，语音按秒计
