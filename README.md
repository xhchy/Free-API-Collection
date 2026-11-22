# Free API Stack

把若干个**免费上游**（Cloudflare Workers AI、Gemini 免费层、gpt4free、free-one-api / pollinations、
Grok Web、Agnes、Edge TTS……）聚合成**一个 OpenAI 兼容入口**：一个地址、一个 key、一套 `/v1`。

- 单端口 `9978` 同时提供 `/v1/models` 与 `/v1/chat/completions`，客户端不用改代码
- 上游真实 key **不进数据库**：NewAPI 里每条渠道的 `key` 字段都写 `vault-only`，
  由本机 keyvault（3997）在转发时注入，后台只能看到占位串
- 全部组件都跑在本机，不需要 Docker，也不需要云服务器
- 附带发布前自检脚本，防止把 key 和本机信息误推到仓库

> **适用**：个人折腾、学习、给自己的一堆工具提供统一出口。
> **不适用**：生产依赖。这些免费额度随时可能限流、改协议或关停，单渠道失效属于常态。
> 使用前请自行阅读各上游的服务条款 —— 账号风险由使用者承担。
---

## 架构

```
                          ┌──────────────────────────────────────────┐
   客户端 ──► 9978 NewAPI ──┤ 全部渠道 base_url = 127.0.0.1:3997/relay/x │
             (one-api.db)  └──────────────────┬───────────────────────┘
                                              │  渠道 key 字段只有 "vault-only"
                                              ▼
                                    3997 keyvault (vault.py)
                                    真 key 放在 keys.json，转发时注入
                                              │
       ┌──────────────┬──────────────┬────────┼──────────────┬──────────────┐
       ▼              ▼              ▼        ▼              ▼              ▼
   3994 bridge    3995 foa      3996 g4f  3998 shim    3999 aiclient   (云上游)
   pollinations   free-one-api  gpt4free  CF 全能力     Grok Web       z.ai / Gemini / Agnes
```

## 端口

| 端口 | 组件 | 作用 |
|---|---|---|
| 3993 | edge-tts | 微软 Edge 朗读，免费中文语音（14 音色） |
| 3994 | foa-bridge | 给 free-one-api 补 `GET /health` |
| 3995 | free-one-api | pollinations 免费层聚合 |
| 3996 | gpt4free | 免费逆向聚合（20+ provider） |
| **3997** | **keyvault** | 真 key 保管 + 请求注入（脱敏核心） |
| 3998 | cf-ai-shim | Cloudflare Workers AI 全能力（视觉 / ASR / TTS / 图像 / 向量 / 重排） |
| 3999 | AIClient2API | Grok Web / Qwen OAuth 账号池 |
| **9978** | **NewAPI** | 对外统一入口 + 管理台 |

全部组件只监听 `127.0.0.1`。要让局域网或公网访问，请自行在前面加反代并做好鉴权。

## 快速开始（Windows）

```bat
REM 0) 取代码（没装 git 就在仓库页点 Code -> Download ZIP）
git clone https://github.com/xhchy/Free-API-Collection.git
cd Free-API-Collection

REM 1) 前置：Python 3.10+、Node 18+

REM 2) 取得 NewAPI 本体（不入库，约 138MB，超过 GitHub 单文件上限）
REM    从 NewAPI 上游仓库的 Releases 下载 new-api-v1.0.0-rc.51.exe，放进本目录

REM 3) 生成你自己的密钥文件
copy vault\keys.example.json vault\keys.json
notepad vault\keys.json          REM 按注释填入自己的 key

REM 4) 装本机组件依赖（只需一次）
cd embedded\aiclient && npm install && cd ..\..

REM 4b) AIClient2API 的配置从模板生成（真实配置含账号 Cookie，不入库）
copy embedded\aiclient\configs\config.json.example embedded\aiclient\configs\config.json
copy embedded\aiclient\configs\provider_pools.json.example embedded\aiclient\configs\provider_pools.json
REM 把 config.json 里的 "SERVER_PORT" 改成 3999（模板默认 3000）
REM 不用 Grok / Qwen 的话这两个文件保持模板原样即可，对应渠道停用就行

REM 5) 一键启动（每个组件一个最小化窗口）
start-all.bat
```

启动后：

- 管理台 <http://127.0.0.1:9978/> —— 首次进入会要求初始化管理员账号
- API `http://127.0.0.1:9978/v1` —— 在管理台新建令牌，请求时带 `Authorization: Bearer <token>`

只起单个组件：双击对应的 `embedded\run-*.bat`。
缺少 `vault\keys.json` 时 `start-all.bat` 会直接拒绝启动，不会带着空 key 假跑起来。
重复运行是安全的：某项的端口已经在监听时会被跳过，不会起出第二份。

### 可选：Python 虚拟环境

gpt4free 和 free-one-api 依赖较多，建议各建一个 venv（`env.bat` 会自动优先使用）：

```bat
cd embedded
python -m venv venv-foa
venv-foa\Scripts\pip install -r foa-bridge\requirements.txt -r free-one-api\requirements.txt

python -m venv venv-g4f
venv-g4f\Scripts\pip install -r g4f\requirements.txt
```

## 密钥怎么配

`vault/keys.json` 分两部分：

- `cloudflare.account_id` / `cloudflare.api_token` —— 供 cf-ai-shim（3998）使用
- `routes.<名字>` —— 每个上游一条，字段含义：
  - `upstream`：真实上游地址
  - `key`：真实 key（会作为 `Authorization: Bearer` 注入）
  - `strip_prefix`：转发前要剥掉的前缀（例如 Gemini 的 OpenAI 兼容端点在 `/v1beta/openai`，
    而 NewAPI 会再拼一层 `/v1`）
  - `proxy`：`true` 表示该路由经本机代理出去，地址取环境变量 `VAULT_PROXY`，
    默认 `http://127.0.0.1:7897`（Clash 默认端口；不用代理就设 `false`）

也可以用同名环境变量覆盖，例如 `CF_API_TOKEN`。模板见 `vault/keys.example.json`。

**这个文件永远不进仓库**（`.gitignore` 已挡）。

## 安全设计：为什么 key 不在数据库里

NewAPI 管理后台会把每个渠道的上游 key 明文展示给管理员。把真 key 填进渠道，
等于任何拿到超管账号的人都能看到全部上游凭证。

这里的做法是：渠道 `key` 一律写 `vault-only`，`base_url` 指向 `127.0.0.1:3997/relay/<路由名>`；
vault 收到请求后剥掉前缀、从 `keys.json` 取真 key 注入 header 再转发。后台只能看到 `vault-only`。

推送代码前请跑一次自检（扫密钥模式、扫本机路径、检查 `.gitignore` 覆盖）：

```bat
python tools\check-secrets.py
python tools\check-secrets.py --strict   REM 连被忽略的文件一起扫
```

返回非 0 就说明有东西该清，别急着 commit。

## 目录

```
new-api-new/
├─ LICENSE                    AGPL-3.0 全文
├─ NOTICE                     第三方归属 + 修改声明
├─ start-all.bat              一键启动 + 端口自检
├─ new-api-v1.0.0-rc.51.exe   NewAPI 本体（第三方，自行下载，不入库）
├─ vault/                     keyvault：keys.json（不入库）+ 模板
├─ embedded/
│  ├─ env.bat                 统一解析 python / node（不写死机器路径）
│  ├─ run-*.bat               各组件单独启动
│  ├─ edge-tts/  foa-bridge/  free-one-api/  g4f/  shim/  aiclient/
└─ tools/check-secrets.py     发布前泄露自检
```

脱敏机制的实现细节见 [`README-自包含部署.md`](README-自包含部署.md)。

## 已知限制

- 免费上游随时 429 / 403 / 改协议，单渠道失效是常态。建议在各渠道上关闭自动禁用
  （`auto_ban = 0`），避免一个模型报错就把整条渠道拖停
- `edge-tts` 依赖微软朗读接口，可能因地区或版本变动失效
- gpt4free 的可用 provider 变化频繁，需要时更新它自己的依赖
- `embedded/free-one-api/data/` 不入库，首次运行会自动生成默认 `config.yaml`
  （默认 token `12345678`，只监听本机；介意就改掉）
- `cf-ai-shim`（3998）启动时读 `vault/keys.json` 的 `cloudflare` 段，
  缺这段会直接报错退出，不会静默返回 401
- 启动脚本按 Windows 编写（bat）；Python 组件本身跨平台，Linux 下需自行写等价脚本
- 上游渠道依赖各自网络条件，部分域名可能需要代理才能访问

## 第三方项目与许可

本仓库内嵌了以下开源项目，各自保留其原始许可证（各目录下的 `LICENSE` 为原文）：

| 目录 | 项目 | 许可证 |
|---|---|---|
| `embedded/free-one-api/` | free-one-api | **AGPL-3.0** |
| `embedded/g4f/` | gpt4free | **GPL-3.0** |
| `embedded/aiclient/` | AIClient2API | **GPL-3.0** |
| `new-api-v1.0.0-rc.51.exe` | NewAPI | **AGPL-3.0**（仅引用上游地址，不随附二进制与源码） |

`vault/`、`embedded/shim/`、`embedded/foa-bridge/`、`embedded/edge-tts/`、`tools/`、
以及各 `*.bat` 启动脚本为本项目自研部分。

## 感谢

 本项目需感谢我们团队的默默奉献，为此在这里我们将列出为Free-API-Collection做出贡献的人/AI：
 
 xhchy/xhtv/szchy/WorkBuddy/deepseek-harness
 
 ## 提供API
 本项目的API目前均由项目主理人：xhchy注册，如果您希望添加API，我们也十分支持您！
 
 **注意**：当我们拿到API后，我们无法保证您的API被封禁账号以及额度消耗完，我们团队正在尽力添加控制API消耗的能力，请您谅解。
 
 提供API请通过以下方式联系我们：
 
 facebook:Xhtv Chen
 
 Proton Mail:cnxhtv@proton.me
 
 Outlook:xh2026new@outlook.com/xhtv2026@hotmail
 
 Gmail:xh2026new@gmail.com
 
 QQ邮箱:xh2026new@qq.com
 
 X:？？？被踢了
 
 微信：amzrain
 
 微博：小红TV
 
 YouTube：XHTV-minecraft
 
## BUG/问题

请通过以下方式跟我们沟通：

 facebook:Xhtv Chen
 
 Proton Mail:cnxhtv@proton.me
 
 Outlook:xh2026new@outlook.com/xhtv2026@hotmail
 
 Gmail:xh2026new@gmail.com

 QQ邮箱:xh2026new@qq.com
 
 X:？？？被踢了
 
 微信：amzrain
 
 微博：小红TV
 
 YouTube：XHTV-minecraft
 
## 许可证

本项目整体以 **GNU Affero General Public License v3.0** 发布，全文见 [`LICENSE`](LICENSE)，
第三方归属与修改声明见 [`NOTICE`](NOTICE)。自研源码文件头带
`SPDX-License-Identifier: AGPL-3.0-or-later`。

由于内嵌了 AGPL-3.0 与 GPL-3.0 代码，而这两种许可证的兼容方向是单向的
（宽松可以并入严格，严格不能退回宽松），整个仓库只能按其中最严格的一个发布。

两点需要注意：

1. **AGPL-3.0 第 13 条**：如果你修改本程序并以网络服务形式提供给他人使用，
   必须向这些用户提供对应的源码。
2. **AGPL-3.0 第 5 条**：修改过的文件需要在文件内注明「已修改」及日期。
   `embedded/aiclient/` 的改动已在文件头标注，`package.json` 等无法写注释的
   JSON 文件在 `NOTICE` 中声明。

NewAPI 本体不由本仓库分发。请从上游仓库获取并遵守其许可证。

## 免责声明

本项目仅供个人学习与研究。所聚合的额度均来自第三方免费服务，
请遵守各上游的服务条款与当地法律。因使用本项目造成的账号封禁、额度异常、
数据丢失或其他损失，作者不承担责任。
