# Hermes Kanban Hollysys Delivery Agent Fleet

这是一套面向可信内部网络和 Ubuntu Linux/AMD64 的 Hermes Agent 0.19.0 多 Agent 部署包。
部署运行固定版本 tag 的官方 Hermes 镜像，通过只读挂载加载无 LLM 的 Python Hollysys
Controller、国内软件源配置和启动检查脚本；不构建定制镜像、不修改 `/opt/hermes`。

人类通过飞书 Dispatcher 启动或废止一次正式交付；Dispatcher 是唯一命令、状态和异常入口，
并通过飞书汇报 Agent/阶段事件。Controller 独立于 Dispatcher/Gateway 会话，持续监听 Kanban
事件、验证 metadata/GitLab 门禁和冻结基线，再创建唯一下一张工作卡：

```text
固定 PRD blob
→ SPEC（write ↔ review，最多 3 次 review）→ 冻结
→ PLAN（write ↔ review，最多 3 次 review）→ 冻结
→ TASKS（write ↔ review，最多 3 次 review）→ 冻结
→ CODE（implement ↔ test ↔ code-review）→ checked-head merge
```

正式交付采用一个 `run_key`、一个共享分支、一个共享 worktree 和一个 MR。GitLab
保存工件、MR、head、门禁与合并事实；Kanban 保存 card、attempt、重试、blocked 和恢复
事实；Controller 只保存可重建的运行控制、事件游标、受管 card ID、幂等请求/操作、依赖
故障和 outbox，不把聊天会话当作运行状态。

## 1. 部署结构

Compose 只有一个 `hermes` service：

- 使用官方Docker镜像 `nousresearch/hermes-agent:v2026.7.20`。
- 以挂载的 root 初始化脚本先检查 .NET SDK 8；容器前台只运行 `sleep infinity`，
  Controller 挂载到官方 `/run/service` 并由 s6 独立监督。Controller 重启不会中断
  Dashboard 或各 Profile Gateway。
- Dashboard 发布到宿主机所有网卡，局域网用户使用同一组固定账号访问。
- 首次缺少镜像时由 Compose 自动拉取，不执行构建。
- 不覆盖镜像 entrypoint，不修改 `/opt/hermes`；只向 `/etc/cont-init.d` 挂载一份
  root 初始化脚本，避免官方 main wrapper 降权后无法执行 APT。
- APT/apt-get、pip、uv、npm、pnpm、Yarn 和常见大型二进制下载默认使用挂载或环境变量
  指定的阿里系镜像。

宿主机目录：

```text
.
├── docker-compose.yaml
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
│   ├── controller/             # controller.db、socket/lock（不进 Git）
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
├── cli/                        # 下载生成，不进 Git；Linux AMD64 CLI
├── skills/                     # 下载生成，不进 Git；GitLab/Lark 官方 Skills
├── scripts/
│   ├── install-external-assets.mjs
│   └── generate_completion_schema.py
├── templates/                  # 中文自动交付、MR、评论、Kanban card 模板
├── schemas/                    # Controller v3 模型生成的严格 metadata v7 Schema
└── tests/                      # 状态机、SQLite、适配和 Compose 契约测试
```

挂载关系：

| 宿主机 | 容器 | 模式 |
| --- | --- | --- |
| `${HERMES_DATA_DIR:-./data}` | `/opt/data` | 可写 |
| `${PROJECTS_DIR:-./projects}` | `/workspace/projects` | 可写 |
| `./cli` | `/opt/cli` | 只读 |
| `./skills` | `/opt/skills` | 只读 |
| `./hollysys_controller` | `/opt/hollysys-controller-src/hollysys_controller` | 只读 |
| `./controller/config.yaml` | `/opt/hollysys-controller/config.yaml` | 只读 |
| `./hollysysctl` | `/usr/local/bin/hollysysctl` | 只读 |
| `./container` | `/opt/fleet/container` | 只读 |
| `./container/ensure-dotnet8.sh` | `/etc/cont-init.d/00-hollysys-dotnet8` | 只读、root 初始化 |
| `./container/services.d/hollysys-controller` | `/etc/services.d/hollysys-controller` | 只读、由 s6 复制到可写 `/run` 的长运行服务 |
| `./container/mirrors/debian.sources` | `/etc/apt/sources.list.d/debian.sources` | 只读 |
| `./container/mirrors/sources.list` | `/etc/apt/sources.list` | 只读 |
| `./container/mirrors/pip.conf` | `/etc/pip.conf` | 只读 |
| `./container/mirrors/uv.toml` | `/etc/uv/uv.toml` | 只读 |
| `./templates` | `/opt/fleet/templates` | 只读 |
| `./schemas` | `/opt/fleet/schemas` | 只读 |

`HERMES_WRITE_SAFE_ROOT=/opt/data:/workspace/projects` 将通用文件写入工具限制在
Hermes 运行数据与 Controller 管理的 checkout/worktree。Hermes 自带的凭据、会话状态
和项目 `.env` denylist 仍然生效；不要把该变量缩回 `/opt/data`，否则 producer 无法
修改工作树，也不要取消变量而放开整个容器文件系统。

容器初始化会把受控 `git`、askpass、credential helper、锁定版本的 `glab` 和
`lark-cli` 以 root-owned、不可由 Agent 修改的 `0555` 文件安装到
`/usr/local/bin`。因此真实 Worker 登录 Shell 不依赖 Compose 传入的 PATH，也不会因
登录 Shell 重置 PATH 而回落到系统 Git。

不要让两个运行中的容器共享同一个 `HERMES_DATA_DIR` 或 `PROJECTS_DIR`。

### 1.1 国内软件源与 .NET SDK 8 启动检查

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

每次容器启动时，`container/ensure-dotnet8.sh` 先检查持久化
`/opt/data/.dotnet` 是否存在任一 8.x SDK。存在即复用；否则检查镜像已有 SDK，或按
Debian 13 官方流程安装 `dotnet-sdk-8.0`，再通过 `/opt/data` 内的临时目录验证并原子
发布到 `.dotnet`。目标目录已存在但无有效 SDK 8 时 fail closed，不会覆盖不明文件。

安装脚本由 s6 在官方 `main-wrapper.sh` 降权前以 root 执行；不要把检查放进 Compose
`command`，因为镜像会用 `s6-setuidgid hermes` 执行该命令，届时已经不能运行 APT。

阿里公共镜像站没有 Microsoft Debian 13 `microsoft-prod` 或 NuGet 公共仓库，因此首次安装 SDK
必须临时访问 `packages.microsoft.com`。脚本无论成功或失败都会清除临时目录，并在
安装成功后卸载仓库配置、删除 Microsoft APT 索引，使后续 apt/apt-get 仍只使用挂载的
阿里 Debian 源。
APT 只承担首次引导；验证后的完整 SDK root 持久化在 `HERMES_DATA_DIR/.dotnet`，
`docker compose up --force-recreate` 或删除容器后不需要再次联网安装。镜像版本由
`HERMES_IMAGE` 明确配置。目标机已有该版本时可使用
`docker compose up -d --pull never hermes`，避免再次拉取。
`dotnet restore` 继续服从目标仓库的 `NuGet.Config`；要求完全内网化时必须由企业提供
NuGet 代理地址，不能把不存在的阿里公共地址写入全局配置。

部署验收不能只运行 Shell 语法和静态 Compose 测试。必须使用全新容器和不含
`.dotnet` 的数据目录启动，确认初始化日志、`dotnet --list-sdks`，并以 `hermes`
用户完成一次 `net8.0` Release build；再次启动还应明确输出 installation skipped。

## 2. 预置 Agent

| Profile | 职责 | GitLab 权限建议 | Feishu Gateway |
| --- | --- | --- | --- |
| `dispatcher` | 飞书命令解析、`hollysysctl` 状态展示、异常交互 | Maintainer（与 Controller 共用） | 是 |
| `prd-writer` | 与人类编写并合入 PRD | Developer | 是 |
| `fde` | 整理现场反馈并创建普通 Issue | Reporter | 是 |
| `spec-writer` | 生成完整 SPEC 集并创建唯一 Draft MR | Developer | 否 |
| `spec-reviewer` | 独立审查 SPEC | Reporter | 否 |
| `planner` | 生成完整 PLAN 集 | Developer | 否 |
| `plan-reviewer` | 独立审查 PLAN | Reporter | 否 |
| `tasker` | 生成 TASKS 集和稳定 DAG | Developer | 否 |
| `task-reviewer` | 审查 SPEC/PLAN/TASKS 一致性 | Reporter | 否 |
| `coder` | 实现、测试和处理代码修复 | Developer | 否 |
| `tester` | 对精确 MR head 独立测试 | Reporter | 否 |
| `code-reviewer` | 对同一 MR head 独立审查代码 | Reporter | 否 |

Controller 不是 Agent Profile，但固定从 Dispatcher 的 0600 `.env` 读取同一份
Maintainer token，并独占正式卡创建、GitLab 门禁复核和
`sha=<checked_head>` 合并动作。准入检查要求 Controller token 与 Dispatcher token
完全一致；Dispatcher 的 Git transport 仍由角色 wrapper 禁止 push。

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
- `HERMES_DASHBOARD_PORT`：Dashboard 在宿主机发布的端口，默认 `9119`。
- `HOLLYSYS_GITLAB_HOST=https://green-git.hollysys.net`、
  `HOLLYSYS_GITLAB_ALLOWED_GROUPS` 和五类 reviewer/tester
  GitLab identity 白名单。
- 默认 Codex OAuth 不需要填写 `OPENAI_API_KEY`。
- 改用 OpenAI-compatible API 时填写 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`。

根 `.env` 既提供 Compose 插值，也通过 `env_file` 传入容器。为避免
`docker-compose.yaml` 堆积大量条目，Dashboard、Controller、模型服务和软件镜像等可配置
环境变量均集中在该文件；固定的容器内部路径仍保留在 Compose。

Dashboard 通过宿主机所有网卡的 `${HERMES_DASHBOARD_PORT:-9119}` 端口发布。本部署假设运行
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

Controller 始终复用 `data/profiles/dispatcher/.env` 中的 `GITLAB_TOKEN`，不再维护
`data/controller/gitlab-token` 第二份凭据。Dispatcher token 必须具有目标群组的
Maintainer 权限；Controller 启动时复用 Dispatcher Profile 的身份、host、allowed
groups、文件所有权和 `0600` 权限校验。轮换 Dispatcher token 后必须在 preflight
模式重新执行 deep preflight，再恢复 active。

五类 identity 白名单可填写 GitLab numeric
user id、username 或显示名，用逗号分隔；留空会使对应 gate 必然失败。

### 3.3 lark-cli

三个 Gateway 使用 lark-cli 主动回复。正式 Hollysys 自动交付的阶段、重试、冻结、
blocked 和完成通知均由 Controller 的持久 outbox 使用 Dispatcher 凭据投递到原会话；
不再为正式卡建立 Hermes Kanban notifier 订阅，也不启动第二个入站消费者。分别复制：

```bash
cp \
  data/profiles/dispatcher/.lark-cli/config/hermes/config.json.example \
  data/profiles/dispatcher/.lark-cli/config/hermes/config.json
chmod 600 data/profiles/dispatcher/.lark-cli/config/hermes/config.json
```

将其中的 `appId`、`appSecret` 替换为该 Profile `.env` 中同一组 Feishu 凭据。配置固定 `defaultAs=bot`、`strictMode=bot`，不允许使用用户身份冒充人类。

`prd-writer` 和 `fde` 做同样处理。实际 `config.json` 已被 Git 忽略。

### 3.4 模型、推理强度与认证

12 个 Profile 默认使用同一模型合同：

```yaml
model:
  provider: openai-codex
  default: gpt-5.6-sol
  base_url: https://chatgpt.com/backend-api/codex

agent:
  reasoning_effort: high
```

`agent.reasoning_effort: high` 是 Agent 主模型的全局默认值，也由未显式设置推理强度的委派调用继承。它比 Hermes 未配置时的 `medium` 使用更多推理 token，并可能增加延迟和账户用量。该值不是不可覆盖的策略锁；会话内显式 `/reasoning` 的优先级更高。辅助任务保留各自的推理默认值。

上游依据：[Hermes v2026.7.20 Reasoning Effort 文档](https://github.com/NousResearch/hermes-agent/blob/3ef6bbd201263d354fd83ec55b3c306ded2eb72a/website/docs/user-guide/configuration.md#reasoning-effort)。

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

上游依据：[Hermes v2026.7.20 AI Provider 文档](https://github.com/NousResearch/hermes-agent/blob/3ef6bbd201263d354fd83ec55b3c306ded2eb72a/website/docs/integrations/providers.md#nous-portal)。

#### 3.4.2 备用：OpenAI-compatible BaseURL + API Key

API 模式是人工切换方案，不是 Codex 认证失败后的自动 fallback。先在实际根 `.env` 中填写：

```dotenv
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_API_KEY=<实际 API Key>
```

再把全部 12 个 Profile 的 `model` 块统一切换；`agent.reasoning_effort: high` 保持不变：

```yaml
model:
  provider: openai-api
  default: deepseek-chat
  base_url: ${OPENAI_BASE_URL}

agent:
  reasoning_effort: high
```

`deepseek-chat` 只是 OpenAI-compatible 示例，实际部署使用服务商提供的模型 ID。仅设置环境变量不会覆盖 Codex provider；必须统一修改 12 个 Profile，禁止不同 Profile 混用 OAuth 与 API Key。API Key 只写入被忽略的根 `.env`，不要写入 `config.yaml`、`docker-compose.yaml` 或文档。

修改根 `.env` 后必须重建容器，`docker compose restart` 不会重新读取环境变量：

```bash
docker compose up -d --force-recreate hermes
```

如果兼容服务不接受 `high`，停止切换并报告兼容性问题，不要静默降低推理强度。

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

上游依据：[Hermes v2026.7.20 Dashboard Analytics 文档](https://github.com/NousResearch/hermes-agent/blob/3ef6bbd201263d354fd83ec55b3c306ded2eb72a/website/docs/user-guide/features/web-dashboard.md#analytics)；[同版本 `show_token_analytics` 配置定义](https://github.com/NousResearch/hermes-agent/blob/3ef6bbd201263d354fd83ec55b3c306ded2eb72a/hermes_cli/config.py#L2052-L2074)。

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
→ tasks-write → tasks-review → implement → test → code-review
→ checked-head merge
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

### 6.5 Protocol v3、运行时合同、单向冻结与代码门禁

- `hollysys_controller.models.CompletionMetadata` 使用 Pydantic `extra=forbid`；
  `scripts/generate_completion_schema.py` 生成仓库 Schema，测试要求两者完全相等。
- 必填 `protocol_version=hollysys-controller/v3`、卡片 mode/iteration、
  `prd_blob_sha` 和全部身份字段；outcome 只允许 pass/fail/cancelled，fail 必须给
  非空 findings，并绑定被检查的 artifact digest 或 MR head。
- Hermes 官方入口仍保存 free-form metadata。Worker 先自检，Controller 在 done 事件后
  强制验证；每个正式角色在 `kanban_complete` 前还必须调用
  `hollysysctl validate-completion`，由 Controller 预检当前 card/run/stage/iteration/
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
  push 使两者失效，从 test 重派。tester 无论 pass/fail，都继续由 code-reviewer
  审查同一 head，完成后才汇总两份结论。
- 必要测试条件（如浏览器、专用硬件或外部环境）经预检确认不具备时，tester 执行
  其余可用检查，并以 `test_disposition=skipped_unavailable` 记录具体原因、证据和
  残余风险；gate marker 同样绑定该处置，不能伪装成已执行。
- tester 与 code-reviewer 对同一 head 都 pass 才进入 checked-head merge。任一 fail
  时，Controller 将两方 findings 通过 `repair_context.kind=code_gate_failure`
  一次性交给 coder，计为第 `n/5` 次修改；修改 push 后重新执行 tester 和
  code-reviewer。第 5 次修改后的版本仍未双通过则结束自动流程，创建 Dispatcher
  异常卡并通过持久 outbox @ 发起人要求人类介入。
- 合并前重读 MR ready、pipeline、讨论、所有最新 gate 和当前 head，只调用
  `sha=<checked_head>` merge。SHA 变化只从 test 重派，绝不无 SHA 重试。
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
`implementation_entry` 还会校验 TASKS ID 唯一、依赖引用存在、
无自依赖/环以及每项恰有一个动作，并拒绝 TASKS 把实现目标指向已冻结上游工件。
TASKS review pass 必须带 approved `implementation_entry`，CODE
review pass 必须带 approved `implementation_completion`，且 reviewer 与 GitLab gate
评论作者一致。下游 migration/deployment/release Gate 不能反向授权修改冻结上游工件，
也不能替代 checked-head、pipeline、独立 review identity 与外部运行证据。

Controller 还按卡持久化 Profile、dispatch key、attempt、session/PID、started/
heartbeat/progress/deadline、worktree/branch/MR/head 和完成接受状态。Hermes 卡使用
`HOLLYSYS_WORKER_REDISPATCH_LIMIT=2`；每次证据闭合后实际发出的 reclaim 立即计入预算，
新 session 只增加 attempt，旧 session 晚到事件不会覆盖当前 attempt。超过进展租约后，
Controller 依次核对 Kanban 状态、session/PID、
Profile、worktree/branch 以及已存在时的 MR/head：进程仍在运行时续租；证据不足时只产生
幂等告警；仅在 PID 已退出且其余身份事实一致时调用 Hermes `reclaim` 请求有限重派。
重派达到 2 次或 Hermes 明确 `gave_up/spawn_auto_blocked` 后归档旧工作卡并进入持久异常。
它不会仅按总运行时长杀死仍可能有真实进展的 Agent。

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
CODE 开始、同一 head 双门禁汇总失败后的第 n/5 次重实现、测试结构化跳过、
修改上限耗尽、checked-head merge 和最终摘要。事件键稳定，
发送成功前保持 pending/retrying；永久 payload 合同错误进入 dead letter，Controller
或 Gateway 重启后按 `next_attempt_at` 指数退避，不重复创建事件。独立 outbox worker
默认每 2 秒投递一次（`HOLLYSYS_OUTBOX_POLL_INTERVAL_SECONDS`），不等待某个 run 的
GitLab reconcile 返回。
`standard` 关闭逐 Agent 开始/完成通知，保留阶段、门禁、阻塞和异常；`minimal` 只保留
终态及必须由人类处理的通知。

#### 人类随时废止

原发起人或 `.env` 中 `HOLLYSYS_ABORT_ADMIN_OPEN_IDS` 配置的管理员可在飞书发送
`废止流程 <run_key> <reason>`。Dispatcher 调用 `abort-request` 后只返回影响说明和
一次性 token；在默认 10 分钟内，同一发送人必须在同一 chat/thread 发送新的
`确认废止 <run_key> <token>` 消息。

确认后 Controller 先持久化 `abort_requested`，阻止正常对账继续发卡，再对运行中受管
卡调用 Hermes 官方 `reclaim` 停止 worker 并归档卡；随后向未合并交付 MR 写入唯一
`[hollysys-aborted:v3]` 审计评论并关闭 MR。branch、worktree、任务、run、评论和日志证据
全部保留供人检查，不删除、不回滚。若 MR 已合并，则终态为
`completed_before_abort`；若外部依赖中断，状态保持 `aborting` 并由后台对账重试。

`exception` 不会自动恢复。根因修复并验证后，只有原发起人或管理员可通过新的飞书消息
调用 `hollysysctl recover`；Controller 归档活动异常卡，以 `state_version` CAS 恢复
`active` 并从同一 run/worktree/MR 重新对账。不能直接改 SQLite 或人工创建下一卡。

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
`[controller-block-rejected:v3]` 并重新释放原卡继续自主决策，不通知人类。
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
新卡创建完成后，旧 blocked 尝试以严格 v7 `outcome=cancelled` 结束并释放新卡。不会
对同一卡盲目 unblock，也不会创建 continuation/恢复 gate。

部署后必须用真实飞书和 Kanban 做一次受控验收，不能用静态检查代替：

1. 从 A 群 @dispatcher 启动 run，强制一张 worker 卡因测试环境权限缺失而阻塞。
2. 确认通知只回到 A 群原话题、真实 @ 原发起人，且包含 run/card 和可执行回复格式。
3. 用非发起人、错误话题和不完整答案分别回复，确认不会恢复。
4. 由原发起人给出有效答案，确认形成 block/resolution 评论、旧尝试完成、单张新 retry 卡自动运行。
5. 重放同一回复，确认不创建重复恢复卡；再验证 `capability`、Gateway 重启以及飞书临时发送失败后的恢复。

## 7. 官方镜像零修改的安全边界

本部署不修改官方 Hermes 内核，硬门禁来自 Controller 对自己创建对象的外部控制：

- `dispatcher.kanban.dispatch_in_gateway: true`、`prd-writer/fde: false` 决定哪个 Gateway 自动轮询 Kanban。
- Dispatcher Feishu toolsets 不含 `kanban`；正式图只由 Controller 通过锁定 Hermes CLI 创建。
- Controller 只认 `managed_cards` 中的 ID，并回读核对 `created_by=hollysys-controller`、
  idempotency、parent、assignee、Skill 和严格 card JSON。
- completion schema 由 worker 自检、Controller 强制验证；非法 done 卡不能推进。
- Controller 与 Dispatcher 使用同一 Maintainer token；checked-head merge 只由
  Controller 流程执行，Agent Git wrapper 额外拒绝 Dispatcher push。
- Memory/Skill 修改仍经过 Hermes 官方 write approval。

仍需客观看待的边界：

- 官方 Kanban 仍允许其他 Profile/人类创建非受管卡；Controller 会忽略，但不能阻止其被
  Gateway 调度。生产 board 应限制 Dashboard/主机访问并监控非受管 ready 卡。
- Hermes CLI/Tool 本身不校验 v7；强制发生在 Controller 消费完成事实时。
- bundled/角色 Skill 的 fleet 定制不可变保护。
- Profile 不等于文件系统 sandbox；Developer/Reporter token、protected branch 和独立
  GitLab 身份仍是权限边界。

Dashboard plugin REST 路由绕过 Basic Auth，且当前端口有意发布到宿主机所有网卡。本部署
以可信内部网络为前提，接受局域网成员可访问 Dashboard 与 plugin REST 的取舍，不围绕公网
攻击模型增加额外配置。Controller 不调用 Dashboard REST/WebSocket，只读 SQLite 事件并通过
官方 CLI 写入。Controller outbox 统一负责阶段进度、blocked、最终成功、工作卡失败熔断和
控制器级故障，均使用稳定事件 key 与 lark-cli idempotency key 去重。

## 8. 全新部署与验收约束

v3 只支持全新部署，不读取、不迁移、不续跑旧 Controller DB、Kanban、session、日志、
cache、pending 或 worktree。新主机只复制明确需要的认证和配置，创建全新的 `data/` 与
`projects/`；旧部署由人类独立结束和处置。不得把旧运行数据挂入 v3 做兼容验证。

首次启动固定 `HOLLYSYS_CONTROLLER_MODE=preflight`，此时 RPC 只提供本地状态、health
和准入检查，不消费 Kanban、不创建卡、不合并。先执行 `hollysysctl preflight` 完成
静态检查，再在用户批准的独立测试项目执行 `hollysysctl preflight --deep`：逐 Profile
验证真实登录 Shell 中 `git`、`glab`、`lark-cli` 精确命中 `/usr/local/bin`，
API identity/membership、HTTPS `ls-remote`、root-owned askpass、无落盘 token、
token-free origin、120 秒内完成的 shallow/blobless/no-checkout clone、Writer 本地空提交身份与
`push --dry-run`，以及
Reviewer 本地拒绝。只有报告全部通过后才将
模式改为 `active` 并全新重建容器。deep preflight 的成功结果和当前 Controller/Profile
凭据契约以不可逆摘要绑定在同一份全新 Controller DB 中；active daemon 启动时强制核验，
缺少 deep 结果、最后一次只做过 static preflight、凭据/allowed group/测试项目发生变化，
或 root-owned Git wrapper 被替换时都会非零退出。任何 token 或准入项目轮换后必须先切回
`preflight`、重新执行 `hollysysctl preflight --deep`，再恢复 `active`。摘要不出现在
health、日志或准入报告中。运行时：

- 容器 healthcheck 调用 `health --probe liveness`，只验证本地 Controller/store/RPC，
  不因 GitLab/Kanban 短时故障重启整个容器；
- 运维与 Dispatcher 调用 `health --probe readiness`，查看最近对账、Kanban/GitLab、
  outbox、merge wait、stale worker、逐 Profile 准入结果和持久 dependency outage；
- 当前版本只支持单机、单 Controller 和固定完整流程；active-active、外部工作流 API
  与跨项目事务不在范围内。

提交或部署前至少执行：

```bash
uv run --with-requirements requirements-test.txt \
  python scripts/generate_completion_schema.py
uv run --with-requirements requirements-test.txt \
  python -m unittest discover -s tests -v
uv run --with-requirements requirements-test.txt \
  ruff check hollysys_controller tests scripts
docker compose config --quiet
git diff --check
```

静态测试与 Compose 解析不是生产 E2E。发布门禁必须额外完成一次真实
PRD→SPEC→PLAN→TASKS→implement→test→code-review→checked-head merge，并验证非法
metadata、三次 review/finalization、冻结文件恢复、同 head 代码门禁、blocked/resume、
verbose Agent 开始/完成通知、两阶段废止、通知幂等、重复答复、依赖断连恢复和
Controller 独立重启。E2E 的 PRD 必须包含遗漏、模糊和
相互矛盾内容，以证明流程不中途回退并最终完成 checked-head merge。
