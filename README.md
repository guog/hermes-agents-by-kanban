# Hermes Kanban Hollysys Delivery Agent Fleet

这是一套面向可信内部网络和 Linux/AMD64 的 Hermes 多 Agent 部署包。v4 从
`nousresearch/hermes-agent:v2026.7.30` 构建派生镜像，固定安装并验证
`jq`、Node 22、npm 10 和 .NET SDK 8.0.423，并只在上游源码指纹完全匹配时应用
terminal Kanban 补丁。派生镜像默认命名为 `hollysys-hermes-agents:latest`。

人类通过飞书 Dispatcher 启动或废止一次正式交付；Dispatcher 是唯一命令、状态和异常入口，
并通过飞书汇报 Agent/阶段事件。Controller 独立于 Dispatcher/Gateway 会话，持续监听 Kanban
事件、验证 metadata/GitLab 门禁和冻结基线，再创建唯一下一张工作卡：

```text
固定 PRD blob
→ SPEC（write ↔ review，最多 3 次 review）→ 冻结
→ PLAN（write ↔ review，最多 3 次 review）→ 冻结
→ TASKS（write ↔ review，最多 3 次 review）→ 冻结
→ CODE（implement → test；仅 test PASS 后 code-review）→ MR Ready / 终态
```

同一 PRD 保留稳定 `source_key`；每次新启动生成随机唯一的 20 位
`run_key/run_generation`、branch 和 worktree。每个 run 只有一个由独立 Controller
身份在首次受控 SPEC push 后创建并持久绑定的 Delivery MR。GitLab
保存工件、MR、head、门禁与合并事实；Kanban 保存 card、attempt、重试、blocked 和恢复
事实；schema 4 Controller store 保存 run/Delivery binding、逐 run_id attempt、
reconcile intent/租约、validation timing、事件游标、幂等请求/操作、依赖故障和
outbox，不把聊天会话当作运行状态。v4 只支持全新部署，不迁移 v3 状态或 legacy MR。

## 1. 部署结构

Compose 有两个独立 service：

- `hermes` 运行 Dashboard、Gateway 与业务 Agent。
- `controller` 独立运行持久 Controller daemon；重启不打断 Hermes service。
- 两者使用同一派生镜像和只包含 Unix socket 的共享 volume。Controller state 使用
  独立宿主目录；Dispatcher Maintainer token 的 `0600` 镜像文件只作为 Controller
  service secret 挂载，不通过容器环境变量扩散。
- Dashboard 发布到宿主机所有网卡，局域网用户使用同一组固定账号访问。
- 镜像构建对 Hermes 源文件做 SHA-256 指纹校验；源码漂移时 fail closed，不模糊套补丁。
- deep preflight 在线准备项目 npm/NuGet 依赖；业务 Profile 只继承离线缓存设置。

宿主机目录：

```text
.
├── docker-compose.yaml
├── Dockerfile                 # 固定 Hermes 版本的 v4 派生镜像
├── .env.example
├── external-assets.json        # 第三方 Skills 与 CLI 的唯一依赖清单
├── package.json
├── package-lock.json           # 锁定 skills CLI 及其 npm 依赖
├── hollysys_controller/        # 无 LLM Controller、RPC、GitLab/Kanban 适配
├── controller/config.yaml      # 无秘密的阶段、Skill、门禁配置
├── container/                  # 只读挂载的启动检查及国内镜像源配置
├── hollysysctl                 # Dispatcher 使用的 Unix Socket CLI
├── requirements-controller.txt # 与官方镜像一致的 Controller Python 依赖
├── requirements-test.txt       # 本地 Schema 语义和完整回归测试依赖
├── data/                       # Hermes 的完整可写 /opt/data
│   ├── scratch/                # 每 attempt 独立安全临时目录
│   ├── cache/                  # preflight 准备的 npm/NuGet 离线缓存
│   └── profiles/
│       └── <profile>/
│           ├── .env.example
│           ├── config.yaml
│           ├── SOUL.md
│           ├── profile.yaml
│           ├── memories/
│           ├── skills/
│           └── home/.gitconfig
├── projects/                   # clone、共享 checkout 和 run worktree
├── controller-data/            # schema 4 controller.db/lock（不进 Git）
├── secrets/                    # Controller 专属 GitLab token（不进 Git）
├── cli/                        # 下载生成，不进 Git；Linux AMD64 CLI
├── skills/                     # 下载生成，不进 Git；GitLab/Lark 官方 Skills
├── scripts/
│   ├── install-external-assets.mjs
│   └── generate_completion_schema.py
├── templates/                  # 中文自动交付、MR、评论、Kanban card 模板
├── schemas/                    # Controller v4 模型生成的严格 completion v8 Schema
└── tests/                      # 状态机、SQLite、适配和 Compose 契约测试
```

挂载关系：

| 宿主机 | 容器 | 模式 |
| --- | --- | --- |
| `${HERMES_DATA_DIR:-./data}` | `/opt/data` | 可写 |
| `${PROJECTS_DIR:-./projects}` | `/workspace/projects` | 可写 |
| `${HOLLYSYS_CONTROLLER_DATA_DIR:-./controller-data}` | `/var/lib/hollysys-controller` | 仅 Controller 可写 |
| `controller-socket` volume | `/run/hollysys-controller` | 两服务共享，仅 RPC socket |
| Controller token secret | `/run/secrets/hollysys_controller_gitlab_token` | 仅 Controller、0400 |
| `./controller/config.yaml` | `/opt/hollysys-controller/config.yaml` | Controller 只读 |
| `./container` | `/opt/fleet/container` | 只读 |
| `./container/ensure-feishu.sh` | `/etc/cont-init.d/02-hollysys-feishu` | 只读、校验并安装固定 Feishu adapter 依赖 |
| `./container/mirrors/debian.sources` | `/etc/apt/sources.list.d/debian.sources` | 只读 |
| `./container/mirrors/sources.list` | `/etc/apt/sources.list` | 只读 |
| `./container/mirrors/pip.conf` | `/etc/pip.conf` | 只读 |
| `./container/mirrors/uv.toml` | `/etc/uv/uv.toml` | 只读 |
| `./templates` | `/opt/fleet/templates` | 只读 |
| `./schemas` | `/opt/fleet/schemas` | 只读 |

`HERMES_WRITE_SAFE_ROOT=/opt/data:/workspace/projects` 保持不变；
`HERMES_SCRATCH_DIR=/opt/data/scratch` 为每个 attempt 提供独立安全子目录；Controller
会在发布工作卡前创建该目录并拒绝符号链接组件。两者将写入限制在
Hermes 运行数据与 Controller 管理的 checkout/worktree。Hermes 自带的凭据、会话状态
和项目 `.env` denylist 仍然生效；不要把该变量缩回 `/opt/data`，否则 producer 无法
修改工作树，也不要取消变量而放开整个容器文件系统。

镜像构建会把受控 `git`、askpass、credential helper、锁定版本的 `glab` 和
`lark-cli` 以 root-owned、不可由 Agent 修改的 `0555` 文件安装到
`/usr/local/bin`。因此真实 Worker 登录 Shell 不依赖 Compose 传入的 PATH，也不会因
登录 Shell 重置 PATH 而回落到系统 Git。

不要让两套 Compose 部署共享同一个 `HERMES_DATA_DIR`、`PROJECTS_DIR` 或
`HOLLYSYS_CONTROLLER_DATA_DIR`。

### 1.1 固定工具链与离线缓存

Compose 同时用标准配置文件挂载和根 `.env` 中的容器环境变量设置软件源；`.env` 通过
`env_file` 直接传入容器：

- Debian 13 `trixie`、`trixie-updates` 和 `trixie-security` 使用
  `https://mirrors.aliyun.com/debian` 与
  `https://mirrors.aliyun.com/debian-security`；旧式 `/etc/apt/sources.list`
  被空文件覆盖，避免与镜像内旧源混用。
- pip 和 uv 使用 `https://mirrors.aliyun.com/pypi/simple/`，并清空默认的
  extra index；npm、Corepack、pnpm 和 Yarn 使用 `https://registry.npmmirror.com/`。
- `NPM_CONFIG_USERCONFIG` 指向随 `./container` 一并只读挂载的
  `container/mirrors/npmrc`；其中启用 `replace-registry-host=always`，使普通 npm
  lockfile 中的官方 registry host 随配置替换。项目显式写死的直链、私有仓库或命令行
  `--index/--registry` 仍具有更高优先级，不会被 Compose 猜测性改写。

根 `.env` 还为明确支持自定义下载地址的常见工具设置
`https://npmmirror.com/mirrors` 下的二进制镜像：

| 类别 | 工具或下载物 | `.env` 配置 | npmmirror 目录 |
| --- | --- | --- | --- |
| Node | nvm、n、node-gyp headers | `NVM_NODEJS_ORG_MIRROR`、`N_NODE_MIRROR`、新旧 node-gyp dist URL | `node` |
| 浏览器测试 | Cypress | `CYPRESS_DOWNLOAD_MIRROR` 和匹配 npmmirror 静态目录的 `CYPRESS_DOWNLOAD_PATH_TEMPLATE` | `cypress` |
| 浏览器测试 | Playwright Chromium | `PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST` | `playwright` |
| 浏览器测试 | 现代 Puppeteer 的 Chrome/Chrome Headless Shell | 两个 `PUPPETEER_*_DOWNLOAD_BASE_URL` | `chrome-for-testing` |
| 浏览器测试 | Puppeteer 旧版 Chromium | `PUPPETEER_DOWNLOAD_HOST` | `chromium-browser-snapshots` |
| 浏览器测试 | `chromedriver` npm 包 | 现代 binary URL 和旧版 CDN URL；元数据仍使用该包默认地址 | `chrome-for-testing`、`chromedriver` |
| 桌面构建 | Electron、electron-builder 工具 | `ELECTRON_MIRROR`、新旧 electron-builder mirror 环境变量 | `electron`、`electron-builder-binaries` |
| 原生依赖 | Sharp 0.32 及更早版本的 libvips、已停止维护的 node-sass | `npm_config_sharp_libvips_binary_host`、`SASS_BINARY_SITE` | `sharp-libvips`、`node-sass` |
| 其他工具 | Prisma engines、Sentry CLI | `PRISMA_ENGINES_MIRROR`、`SENTRYCLI_CDNURL` | `prisma`、`sentry-cli` |

现代 Sharp 把平台预编译包作为 npm optional dependencies 发布，已经由
`registry.npmmirror.com` 覆盖，不再使用旧的 `sharp-libvips` 下载变量。Playwright 只设置
Chromium 专用镜像，因为当前 Debian 13 所需的 Firefox/WebKit 文件在 npmmirror 中不完整；
其余浏览器继续使用 Playwright 默认源。未设置 uv managed Python 的
`UV_PYTHON_INSTALL_MIRROR`：npmmirror 的入口重定向会丢失文件名中编码后的 `%2B`，实际下载
会返回 404。没有公开、兼容且由客户端正式支持的镜像变量时保持工具默认值，不会仅因
npmmirror 上存在同名目录就写入猜测性配置。

这些 `.env` 变量是容器级默认值，目标项目显式设置的环境变量、配置文件或命令行参数可以覆盖。
镜像目录和客户端 URL 规则都可能独立变化；升级 Cypress、Playwright、Puppeteer、
ChromeDriver、Electron、Sharp 或 Prisma 后，应在目标仓库实际执行一次对应的安装命令。

Dockerfile 直接下载并校验 Node 22.18.0 与 .NET SDK 8.0.423 的官方归档 hash；镜像
构建阶段验证 `jq --version`、`node --version`、`npm --version` 和
`dotnet --version`。运行时不再用可漂移的 APT feed 补装 SDK。

Controller 在每个新 run 的唯一 worktree 建立后、第一张业务卡发布前执行
`container/prepare-offline-caches.sh`：对 lockfile 执行在线 `npm ci`，对 solution
执行 `dotnet restore`，分别准备 `/opt/data/cache/npm` 与
`/opt/data/cache/nuget`。Profile terminal 继承 `NPM_CONFIG_OFFLINE=true`、
`NPM_CONFIG_CACHE` 和 `NUGET_PACKAGES`；缺工具或缓存准备失败时 run fail closed。

部署验收不能只依赖静态 Compose 测试。必须构建 linux/amd64 派生镜像，验证源码补丁
只应用于预期 hash，并以全新 schema 4 DB、worktree、随机 run 和新 Draft MR 完成 E2E。

## 2. 预置 Agent

| Profile | 职责 | GitLab 权限建议 | Feishu Gateway |
| --- | --- | --- | --- |
| `dispatcher` | 飞书命令解析、`hollysysctl` 状态展示、异常交互 | Reporter；不得与 Controller token 相同 | 是 |
| `prd-writer` | 与人类编写并合入 PRD | Developer | 是 |
| `fde` | 整理现场反馈并创建普通 Issue | Reporter | 是 |
| `spec-writer` | 生成 SPEC，首次 push 后请求 Controller 发布 Delivery | Developer | 否 |
| `spec-reviewer` | 独立审查 SPEC | Reporter | 否 |
| `planner` | 生成完整 PLAN 集 | Developer | 否 |
| `plan-reviewer` | 独立审查 PLAN | Reporter | 否 |
| `tasker` | 生成 TASKS 集和稳定 DAG | Developer | 否 |
| `task-reviewer` | 审查 SPEC/PLAN/TASKS 一致性 | Reporter | 否 |
| `coder` | 实现、测试和处理代码修复 | Developer | 否 |
| `tester` | 对精确 MR head 独立测试 | Reporter | 否 |
| `code-reviewer` | 对同一 MR head 独立审查代码 | Reporter | 否 |

Controller 不是 Agent Profile。它从仅挂载到 Controller service 的 secret 读取
Dispatcher Maintainer token 的镜像，并独占 branch/MR 创建、run claim、正式卡创建、
GitLab 门禁复核和 `sha=<checked_head>` 合并。准入要求该 token 与 Dispatcher 完全一致；
Dispatcher 的 Git transport 仍由角色 wrapper 禁止 push。

每个 Profile 已直接位于 Hermes 官方运行态目录。`SOUL.md`、Memory 和角色 Skill 不需要复制或安装。Memory 与 Skill 写入继续使用：

```yaml
memory:
  write_approval: true
skills:
  write_approval: true
```

三个 Gateway Profile 预置：

```json
{"gateway_state":"running","desired_state":"running"}
```

官方镜像启动时会扫描带 `SOUL.md` 的 Profile，并自动恢复这三个 Gateway。其他九个 Profile 只作为 Kanban worker，不启动消息 Gateway。

## 3. 首次配置

### 3.1 根 `.env`

复制并编辑：

```bash
cp .env.example .env
chmod 600 .env
```

至少设置：

- `PUID`、`PGID`：Ubuntu 部署用户的 UID/GID。
- `HERMES_CONTAINER_NAME`：容器名；同机并存部署必须使用不同名称。
- `HERMES_DASHBOARD_HOST_PORT`：Dashboard 在宿主机发布的端口，默认 `9119`；
  容器内监听端口固定为 `9119`。
- `HOLLYSYS_GITLAB_HOST=https://green-git.hollysys.net`、
  `HOLLYSYS_GITLAB_ALLOWED_GROUPS` 和五类 reviewer/tester
  GitLab identity 白名单。
- 默认 Codex OAuth 不需要填写 `OPENAI_API_KEY`。
- 改用 OpenAI-compatible API 时填写 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`。

根 `.env` 既提供 Compose 插值，也通过 `env_file` 传入容器。为避免
`docker-compose.yaml` 堆积大量条目，Dashboard、Controller、模型服务和软件镜像等可配置
环境变量均集中在该文件；固定的容器内部路径仍保留在 Compose。
Controller entrypoint 只以 root 完成 UID/GID 对齐和挂载目录初始化，随后使用
`setpriv` 降权为 `hermes` 用户运行配置同步和 daemon。由 Controller 创建的 checkout、
worktree、scratch、socket 与状态文件因此都归属于 `PUID:PGID`；preflight 会拒绝当前
Controller 用户不可写的 `PROJECTS_DIR`。

Dashboard 通过宿主机所有网卡的 `${HERMES_DASHBOARD_HOST_PORT:-9119}` 端口发布。本部署假设运行
在可信内部网络，局域网用户共用以下明文账号，`.env.example` 和实际 `.env` 保持一致：

```dotenv
HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin
HERMES_DASHBOARD_BASIC_AUTH_PASSWORD='Hollysys#1234'
HERMES_DASHBOARD_BASIC_AUTH_SECRET=HollysysInternalDashboardSessionSecret2026
```

`SECRET` 只是 Hermes 要求的 Dashboard 会话签名值。此处的 Dashboard 账号、密码和
session secret 有意明文保存，不作为生产秘密管理；模型 API Key、GitLab token 和飞书
凭据仍按各自章节处理。

内网访问：

```text
# 浏览器打开 http://<部署机内网地址>:9119
```

Hermes Kanban plugin REST 路由不受 Dashboard Basic Auth 保护。对于本项目约定的可信内部
网络，这是接受的部署取舍，不额外要求 loopback、SSH 隧道或逐客户端访问控制。本配置不面向
互联网直接部署。

修改根 `.env` 中的任何容器环境变量后，重建 Hermes 容器以加载：

```bash
docker compose up -d --force-recreate hermes
```

数据、Profile、Kanban、Memory 和 Skill 都位于宿主机挂载目录，重建容器不会清除它们。不要只执行
`docker compose restart`，因为 restart 不会重新读取 Compose 中修改过的环境变量。

### 3.2 Profile `.env`

对 12 个 Profile，将各自 `.env.example` 复制为 `.env` 并填写：

```bash
cp data/profiles/dispatcher/.env.example data/profiles/dispatcher/.env
chmod 600 data/profiles/dispatcher/.env
```

所有 Profile 分别填写：

- `HERMES_PROFILE`（必须与 Profile 目录名完全相同）
- `GITLAB_HOST`
- `GITLAB_ALLOWED_GROUPS`
- 独立的 `GITLAB_TOKEN`

只有 `dispatcher`、`prd-writer`、`fde` 填写独立的：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `API_SERVER_PORT`，默认分别为 `8642`、`8643`、`8644`

不要把 Agent 的 GitLab 或 Feishu 凭据放入根 `.env`。每个 Agent 使用独立、最小权限
token；`terminal.home_mode: profile` 与 `env_passthrough` 将身份、host、allowed groups
和 token 限定在该 Profile。Reviewer/FDE/Dispatcher 即使误配了高权限 token，本地
Git wrapper 仍拒绝 push。

### 3.2.1 Controller Maintainer 凭据

Controller 始终复用 `data/profiles/dispatcher/.env` 中的 `GITLAB_TOKEN`。Compose
要求把同一值镜像到 `${HOLLYSYS_CONTROLLER_GITLAB_TOKEN_FILE}` 指向的 `0600` 文件，
并只挂载给 Controller service。Dispatcher token 必须具有目标群组的 Maintainer
权限；preflight 强制核对两处值一致。轮换 Dispatcher token 后必须同步 secret 镜像，
切回 preflight 模式重新执行 deep preflight，再恢复 active。

五类 identity 白名单可填写 GitLab numeric
user id、username 或显示名，用逗号分隔；留空会使对应 gate 必然失败。

### 3.3 lark-cli

三个 Gateway 使用 lark-cli 主动回复。正式 Hollysys 自动交付的阶段、重试、冻结、
blocked 和完成通知均由 Controller 的持久 outbox 使用 Dispatcher 凭据投递到原会话；
新通知通过 `+messages-reply --markdown` 转为飞书 post 富文本，使用中文标题、分行字段、
短列表和可点击链接。历史 outbox 中的 `text` payload 仍按纯文本重放，不改变旧消息。
`message_id`、原话题、Bot 身份、幂等键和现有 @发起人条件保持不变。不再为正式卡建立
Hermes Kanban notifier 订阅，也不启动第二个入站消费者。Controller 容器启动时会以
三个 Profile `.env` 为权威源，原子生成：

```bash
data/profiles/dispatcher/.lark-cli/config/hermes/config.json
data/profiles/fde/.lark-cli/config/hermes/config.json
data/profiles/prd-writer/.lark-cli/config/hermes/config.json
```

生成文件固定为 `0600`、`defaultAs=bot`、`strictMode=bot`，不允许使用用户身份冒充
人类。static/deep preflight 会逐 Profile 核对 lark-cli 配置与 `.env` 完全一致，并把
该身份合同绑定到 active 模式；不得复制旧部署的实际 `config.json`。实际
`config.json` 已被 Git 忽略。

官方镜像的 Hermes 0.19.0 将 Feishu adapter 声明为可选 extra。容器初始化固定校验并安装
`lark-oapi==1.6.8`、`qrcode==7.4.2` 和 `requests-toolbelt==1.0.0`，下载缓存放在
`/opt/data/.cache/uv`；版本已匹配时跳过安装。缺失或版本漂移时初始化 fail closed，
不允许 Gateway 仅保持进程存活却没有 Feishu adapter。

### 3.4 模型、推理强度与认证

12 个 Profile 默认使用同一模型合同：

```yaml
model:
  provider: openai-codex
  default: gpt-5.6-sol
  base_url: https://chatgpt.com/backend-api/codex

agent:
  reasoning_effort: xhigh
```

`agent.reasoning_effort: xhigh` 是 Agent 主模型的全局默认值，也由未显式设置推理强度的委派调用继承。它比 Hermes 未配置时的 `medium` 使用更多推理 token，并可能增加延迟和账户用量。该值不是不可覆盖的策略锁；会话内显式 `/reasoning` 的优先级更高。辅助任务保留各自的推理默认值。

上游依据：[Hermes v2026.7.30 Reasoning Effort 文档](https://github.com/NousResearch/hermes-agent/blob/cc4cab2f592e60a197e796506de9168f74baf3ea/website/docs/user-guide/configuration.md#reasoning-effort)。

#### 3.4.1 首选：Fleet 级 Codex OAuth

先启动容器，再执行一次不带 `-p` 的根级设备码授权：

```bash
docker compose up -d
docker compose exec -it hermes hermes auth add openai-codex
```

授权凭据写入容器 `/opt/data/auth.json`，对应宿主机 `data/auth.json`。该文件已被 Git 忽略，并随 `data/` 持久化。没有本地同 provider 凭据的 12 个 Profile 会读取这份全局凭据，因此不需要逐 Profile 登录。

检查认证状态：

```bash
docker compose exec hermes hermes auth status openai-codex
docker compose exec hermes hermes auth list openai-codex
```

首次授权或重新授权后重启容器，使三个 Gateway 从干净进程状态读取凭据：

```bash
docker compose restart hermes
```

全局登出会同时影响 12 个 Agent。`data/auth.json` 包含可刷新凭据，备份时必须加密或限制访问，禁止输出内容。若已有 `data/profiles/<profile>/auth.json`，其中的 Codex 凭据会覆盖该 Profile 的全局凭据；迁移时只检查认证状态，不打印 token，也不要未经确认自动删除已有凭据。

上游依据：[Hermes v2026.7.30 AI Provider 文档](https://github.com/NousResearch/hermes-agent/blob/cc4cab2f592e60a197e796506de9168f74baf3ea/website/docs/integrations/providers.md#nous-portal)。

#### 3.4.2 备用：OpenAI-compatible BaseURL + API Key

API 模式是人工切换方案，不是 Codex 认证失败后的自动 fallback。先在实际根 `.env` 中填写：

```dotenv
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_API_KEY=<实际 API Key>
```

再把全部 12 个 Profile 的 `model` 块统一切换；`agent.reasoning_effort: xhigh` 保持不变：

```yaml
model:
  provider: openai-api
  default: deepseek-chat
  base_url: ${OPENAI_BASE_URL}

agent:
  reasoning_effort: xhigh
```

`deepseek-chat` 只是 OpenAI-compatible 示例，实际部署使用服务商提供的模型 ID。仅设置环境变量不会覆盖 Codex provider；必须统一修改 12 个 Profile，禁止不同 Profile 混用 OAuth 与 API Key。API Key 只写入被忽略的根 `.env`，不要写入 `config.yaml`、`docker-compose.yaml` 或文档。

修改根 `.env` 后必须重建容器，`docker compose restart` 不会重新读取环境变量：

```bash
docker compose up -d --force-recreate hermes
```

如果兼容服务不接受 `xhigh`，停止切换并报告兼容性问题，不要静默降低推理强度。

#### 3.4.3 Git identity

producer 的提交身份位于：

```text
data/profiles/{prd-writer,spec-writer,planner,tasker,coder}/home/.gitconfig
```

默认 `.invalid` 邮箱明确标识自动化提交。如果 GitLab 要求已验证提交邮箱，部署前换成管理员创建的已验证 bot alias。

### 3.5 Dashboard Token 分析

12 个 Profile 的 `config.yaml` 均已启用：

```yaml
dashboard:
  show_token_analytics: true
```

Dashboard 左侧 Profile 切换器决定当前查看哪个 Agent。选择 Profile 后，在 Analytics 页面查看该 Profile 的会话数、输入/输出 token、缓存命中、按日和按模型分解。

这些数字来自 Hermes 本地会话历史，只统计返回了可用 usage 的成功主 Agent 响应，是本地统计下限，不是供应商账单。辅助调用、供应商重试、fallback、缺失 usage 的调用及部分缓存数据可能不计入。

上游依据：[Hermes v2026.7.30 Dashboard Analytics 文档](https://github.com/NousResearch/hermes-agent/blob/cc4cab2f592e60a197e796506de9168f74baf3ea/website/docs/user-guide/features/web-dashboard.md#analytics)；[同版本 `show_token_analytics` 配置定义](https://github.com/NousResearch/hermes-agent/blob/cc4cab2f592e60a197e796506de9168f74baf3ea/hermes_cli/config.py)。

## 4. 启动与运维

部署机需要 Node.js 22.20+、npm、git 和 tar；以后声明 ZIP 格式 CLI 时还需要 unzip。首次部署或
`external-assets.json`、`package-lock.json` 发生变化后，先安装并校验外部 Skills 与 CLI，再启动：

```bash
npm ci
npm run assets:install
docker compose config >/dev/null
docker compose up -d
docker compose exec hermes hollysysctl health
```

全新部署复制 `data/profiles/` 时必须保留受 Git 跟踪的
`dispatcher`、`fde`、`prd-writer` 三份 `gateway_state.json`。它们声明
`desired_state=running`；缺失时 s6 只会创建监督目录，不会启动飞书 Gateway。
同时必须保留每个 `data/profiles/<profile>/skills/hollysys-*/SKILL.md`；
排除根目录生成态 `skills/` 时不得使用会递归命中 Profile Skill 的规则。

常用原生命令：

```bash
docker compose ps
docker compose logs --tail=200 hermes
docker compose exec hermes hermes version
docker compose exec hermes hermes profile list
docker compose exec hermes hermes gateway list
docker compose exec hermes hermes -p dispatcher kanban boards list
docker compose exec hermes hollysysctl health
docker compose exec hermes hermes -p coder skills list --enabled-only
docker compose exec hermes hermes -p tester doctor
```

统一使用 `hermes -p <profile>`；本部署不创建 `coder`、`tester` 等 fleet 包装命令。

停止和重启：

```bash
docker compose restart hermes
docker compose stop
docker compose start
docker compose down
```

`docker compose down` 不删除 bind-mounted 的 `data/` 或 `projects/`。

如果某个 Gateway 因配置错误进入 `startup_failed`，修正 `.env` 后显式执行：

```bash
docker compose exec hermes hermes -p dispatcher gateway start
```

官方 Gateway 生命周期会把新的 desired state 写回本地目录。

## 5. 声明式外部 Skills 与 CLI

Git 仓库不保存第三方 Skill、CLI 二进制或许可证副本。所有第三方依赖只在
`external-assets.json` 声明；通用安装器读取清单，在临时目录完成安装和校验后才整体替换本地
`skills/` 或 `cli/`。

当前锁定：

| 工具 | 版本 |
| --- | --- |
| `glab` | 1.108.0 Linux AMD64 |
| `lark-cli` | 1.0.72 Linux AMD64 |
| GitLab `glab` Skill | commit `933cee89...` |
| Lark `lark-shared`、`lark-im` Skills | commit `d6cebd67...` |

Skill 安装复用 [skills CLI](https://github.com/vercel-labs/skills)：

1. 安装器按清单中的完整 commit SHA checkout 来源，避免部署时把可变分支当作锁。
2. `skills@1.5.20` 从精确 checkout 发现并选择清单指定的 Skill，以 Universal、copy、非交互模式安装。
3. 生成 `/opt/skills/<group>/<skill>/SKILL.md` 所需的标准目录；来源 commit 与安装器版本均由 Git 中的清单锁定。

CLI 使用同一份清单的通用条目；每项声明版本、Linux AMD64 下载 URL、归档格式、归档与二进制
SHA-256、安装名和许可证 URL。安装器目前支持 `tar.gz`、ZIP 和直接二进制，不再为某个工具编写专用下载逻辑。

只更新一类依赖时可以运行：

```bash
npm run assets:skills
npm run assets:cli
```

引入新的第三方 Skill：

1. 先用 `npm exec skills -- add <source> --list` 查看上游可安装项并人工审查。
2. 在 `external-assets.json.skills` 增加来源、完整 commit SHA、分组、Skill 名和许可证路径。
3. 运行 `npm run assets:skills`；不修改安装器代码。

引入新的 CLI 时，只在 `external-assets.json.cli` 增加一个通用条目并运行
`npm run assets:cli`。升级 skills CLI 本身时使用
`npm install --save-dev --save-exact skills@<version>`，同时审查并提交 `package-lock.json`。

Compose 将生成的 `cli/`、`skills/` 分别只读挂载到 `/opt/cli`、`/opt/skills`。Profile 中自有的
`data/profiles/<profile>/skills/hollysys-*` 是本部署的角色流程合同，仍属于代码仓库；第三方依赖不会覆盖
Agent 已审批的运行态 Skill。运行中的 Agent 也不下载或更新这些资产。

角色 Skill 使用三层加载机制：

1. Hermes 在系统提示中列出当前 Profile 可用 Skill 的 `name` 和 `description`；三个 Gateway
   Profile 收到自然语言请求时，先依据这份索引判断相关 Skill。
2. 每个 Profile 的 `SOUL.md` 要求在处理本角色请求或 Kanban 卡前确认已经加载自己的
   `hollysys-*` Skill；若卡片没有预加载，则先调用 `skill_view`。
3. 正式 Kanban 卡保存准确的 `skills` 列表。Worker 启动时 Hermes 将每个名称转换为
   `--skills <name>` 并把完整 `SKILL.md` 预加载到该会话，不依赖模型仅凭描述猜测。

容器每次启动时会在官方基础初始化之后、Gateway reconcile 之前，对全部 12 个命名
Profile 执行 Hermes 官方 bundled-Skills 同步。各 Profile 因而拥有同一套镜像内置
Skills；`config.yaml` 的 `skills.disabled` 仍是运行时启用状态的唯一配置约定。部署
就绪检查会验证 12 份 `.bundled_manifest` 一致且对应 Skill 文件完整。

因此，`controller/config.yaml` 的 stage→assignee→Skill 映射必须与 Skill 目录名、
frontmatter `name` 和 SOUL 引用一致。Controller 创建后会从 Kanban DB 回读并验证这些
字段，再释放卡片。Gateway 的首次自然语言命令解析仍属于模型行为；正式推进不
再依赖 LLM continuation。

## 6. 自动开发工作流

### 6.1 中文 SPEC、PLAN、TASKS 模板

三个模板分别位于：

- `templates/spec-template.md`
- `templates/plan-template.md`
- `templates/tasks-template.md`

模板参考 GitHub Spec Kit `main` 提交 `4d3a4281bc63bd2af9f2515bb1036fc38da1294e` 的最新 `spec-template.md`、`plan-template.md`、`tasks-template.md`，并中文化适配当前合同：

- SPEC 按优先级描述可独立验证的用户故事、验收场景、功能需求、成功标准、假设及 PRD 覆盖。
- PLAN 写技术上下文、治理检查、调研决策、接口/数据/安全/测试/回滚设计、真实项目结构和 SPEC 追溯。
- TASKS 使用在完整 PRD TASKS 集内全局唯一且稳定的 `T001` 编号、严格 checklist 行、精确文件路径、显式 `depends_on`、验收/测试、执行波次、无环 DAG 和覆盖矩阵。

producer 必须从对应模板生成工件；reviewer 只把实质性缺项作为问题，不因纯排版差异阻塞交付。

每个 run 还会把源 `docs/prds/<prd>.md` 映射为持久化的单一
`artifact_scope`：SPEC、PLAN、TASKS 只能来自其同名目录
`docs/prds/<prd>/`。Controller 同时比较 writer 的 `head_before_sha → artifact head`
和整个 run 的 `repository_base_sha → current head`：writer 只能改当前阶段工件，reviewer
不能产生新 diff，源 PRD 与其他 PRD 的文档必须保持原 blob。其他 PRD 的问题只能作为
另开 run 的观察，不能进入当前 repair；即使 Agent 把跨 PRD 文件写进 completion，门禁也会
失败关闭。

上游模板：[SPEC](https://github.com/github/spec-kit/blob/4d3a4281bc63bd2af9f2515bb1036fc38da1294e/templates/spec-template.md)、[PLAN](https://github.com/github/spec-kit/blob/4d3a4281bc63bd2af9f2515bb1036fc38da1294e/templates/plan-template.md)、[TASKS](https://github.com/github/spec-kit/blob/4d3a4281bc63bd2af9f2515bb1036fc38da1294e/templates/tasks-template.md)。

### 6.2 启动协议

向 `dispatcher` 发送：

```text
实现 PRD <精确 PRD blob/raw URL> <已合并 PRD MR URL>
```

PRD URL 必须固定到完整 commit SHA。Dispatcher 只做语法转换；GitLab host/group、
项目可读、未归档、PRD 存在、PRD MR 已合入当前默认分支、MR 包含 PRD，以及默认分支
仍是请求版本，都由 `hollysysctl start` 交给 Controller 使用独立 GitLab 身份验证。
Controller 同时保存不可变飞书 origin，并创建合成完成的
`run-init` 根记录和首张 `spec-write` 工作卡。

`run_key` 由下列身份确定：

```text
host + project_id + prd_path + prd_commit_sha
```

启动请求按飞书 `message_id` 幂等；重复消息返回现有 run，已合并返回结果，同一路径的
新 PRD commit 创建新 run。状态查询：

```bash
hollysysctl status --run-key '<run_key>'
```

### 6.3 共享 checkout、分支和 MR

路径固定为：

```text
checkout: /workspace/projects/p<project_id>-<repo-slug>
worktree: /workspace/projects/worktrees/p<project_id>/<run_key>
board:    gitlab-p<project_id>
```

默认分支名：

```text
feature/<prd-basename-max48>-<run-key-suffix>
```

Controller 使用 Dispatcher token 与 `/usr/bin/git` 幂等准备共享 checkout/worktree。Agent
terminal 中的 `git` 固定命中容器启动时安装的 root-owned HTTPS wrapper：只接受
`https://green-git.hollysys.net/<allowed-group>/...`，通过 askpass 返回非空 username
`oauth2`，密码只从当前 Profile 的 `GITLAB_TOKEN` 读取；SSH、明文 HTTP、异主机、
越组访问和 Reviewer push 均 fail closed。token 不进入 argv、origin、Git config 或日志。
所有 producer 共用该分支、worktree 和 MR；reviewer/tester 只评论，不修改产物。

### 6.4 确定性推进与单工作卡

固定完整链路：

```text
spec-write → spec-review → plan-write → plan-review
→ tasks-write → tasks-review → implement → test
→（仅 test PASS）code-review → MR Ready / 终态
```

不创建 continuation、spec/plan/tasks/test/code gate 或 merge 卡。Controller 每两秒
读取各 board 的 append-only `task_events`，每 30 秒执行完整事实对账。阶段由受管
Kanban 卡和 GitLab 实时事实复算；Controller DB 只持久化可重建的 run 状态版本、
重试时刻、checked head、operation/outbox、merge blocker 和 Agent attempt 信封。
进程使用文件锁拒绝第二个 Controller。GitLab、Kanban 或飞书短时故障会写入持久
`dependency_outages`，按配置指数退避并保持 Controller 存活；恢复后关闭同一 outage。
普通 GitLab transient 始终先按 Run 隔离退避；只有达到配置阈值数量的不同 Run 同时
出现 transient，才以相互印证的主机级故障升级为全局 circuit。同一 Run 无论连续失败
多少次都不会单独触发全局熔断。429/auth 直接进入全局 circuit，退避窗口内不按 Run
数重复请求，并只关联实际受到影响的 Run。contract error 只暂停单个 Run，本地
SQLite/配置/状态不变量错误令 daemon
非零退出。每个受影响 Run 只发送一条故障通知和一条引用同一 outage ID 的恢复通知。
只有本地启动、配置、数据库或进程级故障才由 s6 单独重启 Controller。可归属到 run 的
对账错误先写入幂等 outbox，避免只留在容器日志。

每次只创建一张下一阶段实质工作卡，幂等键：

```text
<run>:<stage>:<iteration>:<normal|finalization>:work
```

由于 Hermes CLI 没有通用的“创建 todo 后释放”接口，Controller 使用确定性的两阶段
发布：以目标 assignee 和 `initial-status=blocked` 创建控制器 hold，写入受管 ID，再用
`promote` 释放为 ready/todo。这个短暂状态没有
`kanban_block` 事件，不代表人类阻塞。每个外部步骤都有 operation key；进程在步骤前后
崩溃时，卡片 idempotency、状态回读和全量对账共同避免重复建卡。

### 6.5 Protocol v4、completion v8、单向冻结与代码门禁

- `hollysys_controller.models.CompletionMetadata` 使用 Pydantic `extra=forbid`；
  `scripts/generate_completion_schema.py` 生成仓库 Schema，测试要求两者完全相等。
- 必填 `protocol_version=hollysys-controller/v4`、`source_key`、
  `run_generation`、`context_digest`、`head_before_sha`、
  `deterministic_checks`、卡片 mode/iteration、`prd_blob_sha` 和全部身份字段；
  outcome 只允许 pass/fail/cancelled。v7 输入直接拒绝，不提供兼容别名。
- Hermes 官方入口仍保存 free-form metadata。Worker 先自检，Controller 在 done 事件后
  强制验证；每个正式角色在 `kanban_complete` 前还必须调用
  `hollysysctl card-context` 获取精简受信上下文，以 `completion-template` 生成当前
  stage/mode/outcome 的合法对象，再调用 `hollysysctl validate-completion`，由
  Controller 预检当前 card/run/stage/iteration/
  worktree/branch/MR 上下文。非法对象不推进，创建同阶段新 attempt，最多自动重试两次。
- 非 TEST completion 必须省略 `test_disposition` 和 `skip_reason`（兼容输入可为
  JSON `null`）；lint、build 和文档检查写入 `verification`。TEST 使用
  `test_disposition=executed` 时也不得携带非空 `skip_reason`。仓库 JSON Schema 和
  Pydantic 对这些组合执行同一接受/拒绝矩阵。
- run 启动时固定 PRD blob SHA。SPEC/PLAN/TASKS gate 会按配置 pattern 枚举 artifact
  commit 上的完整路径集合，重算排序后的 `<path>\0<blob-sha>\n` digest，并核对
  MR note 作者、artifact commit 与 card ID。
- run 同时固定默认分支的 `repository_base_sha`。SPEC、PLAN、TASKS 和 CODE 都是
  对现有企业 MES 的客户定制：先读取该基线上的代码、文档、配置、接口和测试，再
  选择扩展现有功能、修改现有功能或两者组合。四个 authoring pass 必须提交
  `repository_evidence`，列出实际检查路径、现有能力、变更类型和复用决策；把仓库
  当绿地项目、重复造已有能力或无证据新建平行框架会被对应 reviewer 判为缺陷。
  Controller 使用 `git cat-file` 验证每个检查路径确实存在于该 base commit，虚构、
  glob、绝对路径和跨目录引用不能推进。
- 每个文档阶段最多三次 review，第一次计入。第 1/2 次 fail 将精确 findings 通过
  `repair_context` 交回本阶段 writer；任意一次 pass 立即以 `reviewed` 冻结并前进。
  第 3 次 fail 只创建一次 `mode=finalization` producer 工作，不再进行第 4 次 review。
- finalization 尽量修复第三次 findings，在工件和唯一
  `HOLLYSYS-FORCED-ADVANCE` 评论中记录最终取舍、未解决 findings、残余风险、影响和
  可逆方式。Controller 对账第三次 review 评论、决策评论、commit/paths/digest 后以
  `forced_after_review_limit` 冻结并前进。
- 后续阶段不得修改 PRD 或已冻结的 SPEC/PLAN/TASKS。Controller 在发卡、消费
  completion 和合并前逐项复算基线；若当前阶段误改冻结文件，只向当前 producer
  派发恢复基线修复，不重开上游阶段。
- test/code-review note 必须由配置的独立身份发布，并与当前 MR 同一 `head_sha`；任何
  push 使两者失效，从 test 重派。只有 tester PASS 才创建 code-review；tester FAIL
  直接将 findings 交回 coder，不运行或沿用旧 head 的 code-review。
- 必要测试条件（如浏览器、专用硬件或外部环境）经预检确认不具备时，tester 执行
  其余可用检查，并以 `test_disposition=skipped_unavailable` 记录具体原因、证据和
  残余风险；gate marker 同样绑定该处置，不能伪装成已执行。
- tester PASS 后，code-reviewer 对同一 head PASS 即将 MR 改为 Ready 并进入
  `completed_ready`；Controller 不自动合并。第 5 次修改后 tester PASS、code-reviewer
  FAIL 时同样将 MR 改为 Ready，进入 `completed_with_findings`；tester FAIL 时不运行
  code-review、不改变 MR 状态，进入 `completed_test_failed`。额度内任一门禁失败均以
  `repair_context.kind=code_gate_failure` 交回 coder，修改 push 后重新从 tester 开始。
- Ready 前后均重读当前 MR/head；SHA 变化只从 test 重派，不用旧 head 门禁完成流程。
- 若 MR 在 Controller 已实际提交的同-head merge operation 之外被人工或其他自动化合并，流程
  仍会停止调度并落为 `completed`，但固定标记 `source=external,
  compliance=unverified` 并通知人类，不把同-head review 误当成完整合并时证据。

五类语义 Gate 固定为 `implementation_entry`、`implementation_completion`、
`migration_execution`、`deployment_entry` 和 `release_acceptance`。携带 Gate 时必须
同时给 decision、reviewer、带时区时间、理由、证据引用、冻结 TASKS paths/commit/digest、
`contract_refs` 和 `requirement_ids`；Controller 重算 digest、读取冻结 TASKS 正文并
确认引用真实存在。每个 Gate 至少引用一条当前交付 MR 的精确 `#note_<id>` URL；
Controller 回读 note，要求其作者与 `gate_reviewer=id:<numeric-id>` 一致，并校验同一
note 中的 `HOLLYSYS-SEMANTIC-GATE` marker 绑定 run、phase、decision、TASKS commit 和
digest；仅有自报 reviewer 或未验证的同项目 URL 会 fail closed。
TASKS Writer/Reviewer 共用 `validate-artifact` 的相同 validator；
`implementation_entry` 还会校验 TASKS ID 唯一、依赖引用存在、
无自依赖/环以及每项恰有一个动作，并拒绝 TASKS 把实现目标指向已冻结上游工件。
TASKS review pass 必须带 approved `implementation_entry`，CODE
review pass 必须带 approved `implementation_completion`，且 reviewer 与 GitLab gate
评论作者一致。下游 migration/deployment/release Gate 不能反向授权修改冻结上游工件，
也不能替代 checked-head、pipeline、独立 review identity 与外部运行证据。

Controller 以 Hermes `task_events.run_id` 为 attempt 权威键，逐次持久化 Profile、
dispatch key、session/PID、started/blocked/completion/exited、恢复来源、
heartbeat/progress/deadline、worktree/branch/MR/head 和完成接受状态。heartbeat
只更新 liveness，不续 progress lease。未带 `worker_session_id` 的 Hermes lifecycle
事件以 `task_events.run_id` 合成稳定 attempt identity，避免多个重试在健康页中坍缩为
一个永不退出的 worker。Hermes 卡使用
`HOLLYSYS_WORKER_REDISPATCH_LIMIT=2`；每次证据闭合后实际发出的 reclaim 立即计入预算，
新 session 只增加 attempt，旧 session 晚到事件不会覆盖当前 attempt。15 分钟无结构化
进展只报 `slow_alive`，并明确提示“仍在运行、无需立即处理”；30 分钟且 heartbeat
正常只报 `stuck_alive`，提示“长时间无新进展、暂不重派”。`liveness_unconfirmed`
则明确说明证据尚未闭合、人类无需手动重派。只有 heartbeat 与 progress 同时超时，
Controller 才在完整核对 Kanban run/session/PID、Profile、
worktree/branch/MR/head 后，通过 Hermes PID namespace 内的 Unix Socket Supervisor
执行 probe/terminate；Socket、身份或进程证据不完整时只报 `liveness_unconfirmed`。
Supervisor 确认该 attempt 的 Worker 与后代均退出后，Controller 再做完整 CAS，并调用
带 `--expected-run-id/--expected-worker-pid` 的 `reclaim` 请求有限重派。
重派达到 2 次或 Hermes 明确 `gave_up/spawn_auto_blocked` 后归档旧工作卡并进入持久异常。
它不会仅按总运行时长杀死仍可能有真实进展的 Agent。

所有 Kanban worker 强制使用 Hermes `-Q` 机器退出合同，不能走会在 API 失败后仍返回
`rc=0` 的人类展示 `-q` 分支。模型限流、billing、transport timeout、provider overload、
上游 5xx 和无状态码临时故障在内建重试耗尽后返回 `EX_TEMPFAIL`；Hermes 将该 attempt
记录为 `rate_limited`，默认冷却 5 分钟后再探测，且不消耗工作卡 redispatch/failure
预算。认证、配置、工件合同、终端工具遗漏等确定性失败仍按原有限预算熔断。

event poll 只持久化 lifecycle/cursor 和 reconcile intent；独立 worker 通过持久租约
消费并合并同一 run 的重复 intent，崩溃后可重新领取。
`HOLLYSYS_RECONCILE_WORKERS=4` 允许不同 run 有限并发，但同一 run 同时只有一个 reconcile。
外部 I/O 期间不持有全局或 run 锁；返回后以 `state_version` 和 checked head 丢弃陈旧结果，
因此一个 GitLab 超时不会阻塞其他 run、本地 status、outbox 或先持久化人工废止。
Merge 默认每 30 秒重查，Draft 宽限 600 秒，其余 blocker 最长 3600 秒，分别由
`HOLLYSYS_MERGE_WAIT_RETRY_SECONDS`、`HOLLYSYS_MERGE_DRAFT_GRACE_SECONDS` 和
`HOLLYSYS_MERGE_BLOCKER_TIMEOUT_SECONDS` 控制。

`hollysysctl status --run-key ...` 只读本地 Controller store 与 Kanban，返回精确
run state/version/next retry、phase/stage、review 已用/剩余次数、当前 Agent/card/mode、
attempt/session 和持久 merge blocker；它不做 GitLab 网络审计，因此外部依赖超时不会
阻塞人类状态查询或废止。

### 6.6 飞书进度、阻塞与持久 outbox

Dispatcher 不把普通 heartbeat 转发到飞书。试运行默认
`HOLLYSYS_NOTIFICATION_LEVEL=verbose`，Controller 在每个 Agent 被领取/开始以及完成协议
被接受或拒绝时分别通知原会话，并继续在下列业务事件写入持久
outbox：run 受理、阶段开始、每次 review fail、第三次失败进入 finalization、阶段冻结、
CODE 开始、Tester/Code Reviewer 失败后的第 n/5 次重实现、测试结构化跳过、
三类 CODE 终态、MR Ready 和最终摘要。事件键稳定，
发送成功前保持 pending/retrying；永久 payload 合同错误进入 dead letter，Controller
或 Gateway 重启后按 `next_attempt_at` 指数退避，不重复创建事件。独立 outbox worker
默认每 2 秒投递一次（`HOLLYSYS_OUTBOX_POLL_INTERVAL_SECONDS`），不等待某个 run 的
GitLab reconcile 返回。
`standard` 关闭逐 Agent 开始/完成通知，保留阶段、门禁、阻塞和异常；`minimal` 只保留
终态及必须由人类处理的通知。

人类消息使用固定 Markdown 模板：第一行先给结论，协议值保留原值并追加中文解释，
run/Card/SHA 使用行内代码；MR、审查记录、流水线、Job 和提交使用说明目标的链接文字，
禁止使用“链接 1”“证据 2”。SPEC、PLAN、TASKS 的“阶段轮次”按 write→review 配对
计算，依次显示 `1/3`、`2/3`、`3/3`，分子为已使用次数；Hermes 重派 attempt 不增加
阶段轮次，非文档阶段如需展示则明确写“执行尝试”。CODE 修改仍单独显示 `n/5`。
findings、决策和风险只发送简短中文摘要，超长原文以省略号收敛。完整示例位于
`templates/feishu-messages.md`。

#### 人类随时废止

原发起人或 `.env` 中 `HOLLYSYS_ABORT_ADMIN_OPEN_IDS` 配置的管理员可在飞书发送
`废止流程 <run_key> <reason>`。Dispatcher 调用 `abort-request` 后只返回影响说明和
一次性 token；在默认 10 分钟内，同一发送人必须在同一 chat/thread 发送新的
`确认废止 <run_key> <token>` 消息。

确认后 Controller 先持久化 `abort_requested`/`aborting`，阻止正常对账继续发卡，再经
Hermes Supervisor 确认运行中 Worker 与后代真实退出，随后以 attempt CAS reclaim 并归档
卡；reclaim 与 archive 在同一 Kanban 事务完成，不产生瞬时可重派窗口。尚未 claim 的
卡也使用 unclaimed CAS 归档，若并发 claim 则保持 `aborting` 并转入 Supervisor 路径。
终止失败时保持 `aborting`，不关闭交付或伪造成功。确认退出后再向未合并交付 MR 写入唯一
`[hollysys-aborted:v4]` 审计评论并关闭 MR。branch、worktree、任务、run、评论和日志证据
全部保留供人检查，不删除、不回滚。若 MR 已合并，则终态为
`completed_before_abort`；若外部依赖中断，状态保持 `aborting` 并由后台对账重试。

`exception` 不会自动恢复。根因修复并验证后，只有原发起人或管理员可通过新的飞书消息
调用 `hollysysctl recover`；Controller 归档活动异常卡，以 `state_version` CAS 恢复
`active` 并从同一 run/worktree/MR 重新对账。若异常来源工作卡已归档且不存在其他活动
Worker，Controller 会使用当前受信上下文重新派发同阶段 attempt，并返回
`continuation=work-reissued`；不得留下 `active` 但无工作卡的空转状态。不能直接改
SQLite 或人工创建下一卡。

#### Blocked 的原渠道人类闭环

正式 Hollysys 工作卡不建立 Kanban notifier 订阅。Controller 从 run 根记录读取原
`message_id/chat_id/thread_id/initiator_open_id`，所有业务事件通过 SQLite outbox 和
稳定 idempotency key 使用 Dispatcher 的 lark-cli 凭据回复原消息/话题。

业务遗漏、歧义和矛盾不得阻塞。人类阻塞只用于：

- 缺少权限、凭据、环境或只能由人执行的能力；
- 自动重试不安全；
- 破坏性操作需要明确授权。

普通依赖使用 Kanban 父子关系，缺陷使用 `fail` 留在当前阶段处理，不能滥用 blocked。
Worker 阻塞前先在卡片写入幂等 `[human-block:v1]` 评论，包含 run、stage、Agent、
脱敏证据、一个明确动作和恢复校验条件，随后调用 typed `kanban_block`。Controller
看到 blocked 事实和有效评论后写入持久 outbox；通知回到原聊天/话题，并使用发起人的
`open_id` 真实 mention：

`kind` 只允许 `permission`、`credential`、`environment`、`unsafe_retry` 或
`destructive_approval`。Controller 会拒绝业务歧义或字段不完整的 block，写入
`[controller-block-rejected:v4]` 并将该 attempt 置为 `exception`；认证、工具、
能力或合同错误分别进入 `human_blocked`、`retry_wait` 或 `exception`，不会立即
promote 或重新释放原卡。
其中 `environment` 与 `destructive_approval` 必须额外给出
`gate_phase=migration_execution|deployment_entry|release_acceptance`、冻结
`requirement_ids` 和
`contract_refs`；发布/完成门禁不得被扩张成禁止仓库内编码、测试或脚本准备。

```text
@发起人 自动交付在 <stage> 暂停：<一个问题或动作>。
请回复本消息并 @dispatcher：
处理阻塞 <run_key> <card-id> <答案/已完成动作>
```

Dispatcher 不能直接恢复。它把真实 sender/chat/thread/message/answer 交给
`hollysysctl resolve`。Controller 验证 root origin、blocked 状态和匹配的
`[human-block:v1]` 评论，以 `block_id + message_id` 幂等创建一张同阶段新 attempt；
actor 固定为 `hollysys-controller`。新卡创建完成后，旧 blocked 尝试以 completion v8
`outcome=cancelled` 结束，新卡取得新的 Hermes `run_id`/attempt。不会对同一卡盲目
unblock，也不会创建 continuation/恢复 gate。Hermes 会把 Controller 正常发布初始
blocked 卡的 `promote` 同样记录为 `promoted_manual`；Controller 只在存在匹配且处于
执行中或已完成的 durable `release` operation 时认可这一次发布。额外 promotion 仍必须有匹配的人类
`[human-resolution:v1]`，否则进入异常。

部署后必须用真实飞书和 Kanban 做一次受控验收，不能用静态检查代替：

1. 从 A 群 @dispatcher 启动 run，强制一张 worker 卡因测试环境权限缺失而阻塞。
2. 确认通知只回到 A 群原话题、真实 @ 原发起人，且包含 run/card 和可执行回复格式。
3. 用非发起人、错误话题和不完整答案分别回复，确认不会恢复。
4. 由原发起人给出有效答案，确认形成 block/resolution 评论、旧尝试完成、单张新 retry 卡自动运行。
5. 重放同一回复，确认不创建重复恢复卡；再验证 `capability`、Gateway 重启以及飞书临时发送失败后的恢复。

## 7. 固定派生镜像的安全边界

本部署从固定 Hermes 版本 tag 构建派生镜像，并在构建时对预期 Hermes 源文件 hash 做指纹校验；
只有精确匹配时才应用 terminal Kanban 补丁。硬门禁来自 Controller 对自己创建对象的外部控制：

- `dispatcher.kanban.dispatch_in_gateway: true`、`prd-writer/fde: false` 决定哪个 Gateway 自动轮询 Kanban。
- Dispatcher Feishu toolsets 不含 `kanban`；正式图只由 Controller 通过锁定 Hermes CLI 创建。
- Controller 只认 `managed_cards` 中的 ID，并回读核对 `created_by=hollysys-controller`、
  idempotency、parent、assignee、Skill 和严格 card JSON。
- completion schema 由 worker 自检、Controller 强制验证；非法 done 卡不能推进。
- Controller 使用 Dispatcher Maintainer token 的只读 secret 镜像，preflight 强制
  两处值一致；Draft MR 创建与 ready 切换只由 Controller 流程执行，
  Agent Git wrapper 额外拒绝 Dispatcher push。
- Memory/Skill 修改仍经过 Hermes 官方 write approval。
- Hermes 补丁保证成功 `kanban_complete`/`kanban_block` 后停止同批后续业务工具和下一次
  模型调用；`worker_exited` 只由真实 `waitpid`/reap 路径产生，不再把 terminal 工具成功
  当作进程退出。补丁还强制 Kanban worker 使用 `-Q`
  退出合同，将明确的临时 provider 失败映射为 `EX_TEMPFAIL` 冷却重试。Gateway 会把
  当前 `MessageEvent.message_id` 绑定到 session context，避免 Dispatcher 首次启动时拿到
  空消息 ID。`delegate_task` 子会话只返回盘点结果：其 Kanban 变更工具从 schema 中移除，
  executor 还会拒绝子会话的 terminal lifecycle 调用；父 worker 的 completion provenance
  使用 Dispatcher `run_id` 派生的稳定 attempt identity，而不是会被子会话覆盖的进程级
  session 环境变量。任一补丁源文件指纹不匹配时镜像构建直接失败。
- 固定版本的 Feishu Python runtime 直接烘焙进派生镜像；容器 init 仍逐版本校验并保留
  缺包兜底，但正常全新启动不再等待 PyPI，也不会在 Gateway 尚未启动时把 Controller
  liveness 误当成完整飞书就绪。

仍需客观看待的边界：

- 官方 Kanban 仍允许其他 Profile/人类创建非受管卡；Controller 会忽略，但不能阻止其被
  Gateway 调度。生产 board 应限制 Dashboard/主机访问并监控非受管 ready 卡。
- Hermes CLI/Tool 本身不校验 completion v8；强制发生在 Controller 消费完成事实时。
- bundled/角色 Skill 的 fleet 定制不可变保护。
- Profile 不等于文件系统 sandbox；Developer/Reporter token、protected branch 和独立
  GitLab 身份仍是权限边界。

Dashboard plugin REST 路由绕过 Basic Auth，且当前端口有意发布到宿主机所有网卡。本部署
以可信内部网络为前提，接受局域网成员可访问 Dashboard 与 plugin REST 的取舍，不围绕公网
攻击模型增加额外配置。Controller 不调用 Dashboard REST/WebSocket，只读 SQLite 事件并通过
官方 CLI 写入。Controller outbox 统一负责阶段进度、blocked、最终成功、工作卡失败熔断和
控制器级故障，均使用稳定事件 key 与 lark-cli idempotency key 去重。

## 8. 全新部署与验收约束

v4 只支持全新部署，不读取、不迁移、不续跑旧 Controller DB、Kanban、session、日志、
cache、pending 或 worktree。新主机只复制明确需要的认证和配置，创建全新的 `data/` 与
`projects/`；旧部署由人类独立结束和处置。不得把旧运行数据挂入 v4 做兼容验证，
旧 MR（包括 `!53`）也不得导入或恢复为 v4 Delivery binding。

首次启动固定 `HOLLYSYS_CONTROLLER_MODE=preflight`，此时 RPC 只提供本地状态、health
和准入检查，不消费 Kanban、不创建卡、不合并。先执行 `hollysysctl preflight` 完成
静态检查，再在用户批准的独立测试项目执行 `hollysysctl preflight --deep`：逐 Profile
验证真实登录 Shell 中 `git`、`glab`、`lark-cli` 精确命中 `/usr/local/bin`，
API identity/membership、HTTPS `ls-remote`、root-owned askpass、无落盘 token、
token-free origin、120 秒内完成的 shallow/blobless/no-checkout clone、Writer
通过 `mktree + commit-tree` 验证的本地空提交身份与
`push --dry-run`，以及
Reviewer 本地拒绝；同时核对三个 lark-cli bot 配置与各自 Profile `.env` 一致，以及
Controller 对 `PROJECTS_DIR` 可写。只有报告全部通过后才将
模式改为 `active` 并全新重建容器。deep preflight 的成功结果和当前 Controller/Profile
凭据契约以不可逆摘要绑定在同一份全新 Controller DB 中；active daemon 启动时强制核验，
缺少 deep 结果、最后一次只做过 static preflight、凭据/allowed group/测试项目发生变化，
或 root-owned Git wrapper 被替换时都会非零退出。Hermes 容器和 Supervisor Socket 就绪后，
必须再执行 `hollysysctl preflight --deep --require-supervisor`；active 启动只接受这次
带 Supervisor 协议证明的深预检摘要。任何 token 或准入项目轮换后必须先切回
`preflight`、重新执行上述深预检，再恢复 `active`。摘要不出现在
health、日志或准入报告中。运行时：

`hollysysctl preflight` 必须连接 Controller RPC，由已降权的 daemon 执行；不要在
Controller 容器内另起本地 `ControllerService`，否则 Profile HOME 中由准入工具创建的
文件会继承 `docker compose exec` 的 root 身份，导致 active 凭据合同漂移。

- 容器 healthcheck 调用 `health --probe liveness`，只验证本地 Controller/store/RPC，
  不因 GitLab/Kanban 短时故障重启整个容器；
- `start` 的 GitLab/workspace/offline-cache 初始化超过同步窗口时，RPC 返回
  `request_status=running` 的只读初始化快照，后台继续同一个幂等请求；客户端不会因
  120 秒终端超时重复创建任务；
- 运维与 Dispatcher 调用 `health --probe readiness`，查看最近对账、Kanban/GitLab、
  outbox、merge wait、stale worker、逐 Profile 准入结果、`model_provider` 冷却状态和持久
  dependency outage；真实 waitpid 上报的 provider `EX_TEMPFAIL`（exit 75）会立即进入
  不消耗 redispatch budget 的持久冷却，冷却结束后才释放同一卡片重试；
- 静态和深度预检逐 Profile 校验阶段必需的 `hollysys-*` Skill 以及共享 `glab`
  Skill，并将文件摘要绑定到 active 启动契约；缺失、符号链接或预检后变更都会
  fail closed；
- 当前版本只支持单机、单 Controller 和固定完整流程；active-active、外部工作流 API
  与跨项目事务不在范围内。

提交或部署前至少执行：

```bash
uv run --no-project --with-requirements requirements-test.txt \
  python scripts/generate_completion_schema.py
uv run --no-project --with-requirements requirements-test.txt \
  pytest -q
uv run --no-project --with-requirements requirements-test.txt \
  ruff check hollysys_controller tests scripts
docker compose config --quiet
git diff --check
```

派生镜像构建后还必须执行
`scripts/test-worker-supervisor-containers.sh hollysys-hermes-agents:latest`；该门禁以临时
Hermes/Controller 容器和临时卷验证独立 PID namespace、Socket probe 及完整进程树终止，
不会加入或重建现有 Compose 项目。

静态测试与 Compose 解析不是生产 E2E。发布门禁必须额外完成一次真实
PRD→SPEC→PLAN→TASKS→implement→test→按条件 code-review→MR Ready/终态，并验证非法
metadata、三次 review/finalization、冻结文件恢复、同 head 代码门禁、blocked/resume、
verbose Agent 开始/完成通知、两阶段废止、通知幂等、重复答复、依赖断连恢复和
Controller 独立重启。E2E 的 PRD 必须包含遗漏、模糊和
相互矛盾内容，以证明流程不中途回退并准确进入三类 CODE 终态。
