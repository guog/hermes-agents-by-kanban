# Hermes Kanban Hollysys Delivery Agent Fleet

这是一套面向 Ubuntu Linux/AMD64 的 Hermes Agent 0.19.0 多 Agent 部署包。部署运行 digest 锁定的官方 Hermes 镜像，通过只读挂载加载无 LLM 的 Python Hollysys Controller；不构建定制镜像、不修改 `/opt/hermes`、不打运行时补丁。

人类通过飞书 Dispatcher 启动一次正式交付；Dispatcher 只调用 `hollysysctl`。Controller
监听 Kanban 事件、验证 metadata/GitLab 门禁和重试预算，再创建唯一下一张工作卡：

```text
PRD → SPEC → SPEC review → PLAN → PLAN review → TASKS → TASK review
    → code + self-test → test → code review → checked-head merge
```

正式交付采用一个 `run_key`、一个共享分支、一个共享 worktree 和一个 MR。GitLab
保存工件、MR、head、门禁与合并事实；Kanban 保存 card、attempt、重试、blocked 和恢复
事实；Controller 只保存事件游标、受管 card ID、幂等请求/操作和 outbox，不保存独立当前阶段。

## 1. 部署结构

Compose 只有一个 `hermes` service：

- 使用 Linux AMD64 manifest digest 固定的官方 `nousresearch/hermes-agent:v2026.7.20`。
- 以 `python -m hollysys_controller.daemon` 作为容器前台主进程；Controller 退出会触发容器重启，Gateway 和 Dashboard 仍由官方 s6 监督。
- Dashboard 只发布到宿主机 `127.0.0.1`，远程访问使用 SSH 隧道。
- 首次缺少镜像时由 Compose 自动拉取，不执行构建。
- 不覆盖镜像 entrypoint，不向 `/opt/hermes` 或 `/etc/cont-init.d` 挂载文件。

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
├── hollysysctl                 # Dispatcher 使用的 Unix Socket CLI
├── requirements-controller.txt # 与官方镜像一致的 Controller Python 依赖
├── data/                       # Hermes 的完整可写 /opt/data
│   ├── controller/             # token、controller.db、socket/lock（不进 Git）
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
├── schemas/                    # Controller 模型生成的严格 metadata v3 Schema
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
| `./templates` | `/opt/fleet/templates` | 只读 |
| `./schemas` | `/opt/fleet/schemas` | 只读 |

不要让两个运行中的容器共享同一个 `HERMES_DATA_DIR` 或 `PROJECTS_DIR`。

## 2. 预置 Agent

| Profile | 职责 | GitLab 权限建议 | Feishu Gateway |
| --- | --- | --- | --- |
| `dispatcher` | 飞书命令解析、`hollysysctl` 状态展示、异常交互 | Reporter | 是 |
| `prd-writer` | 与人类编写并合入 PRD | Developer | 是 |
| `fde` | 整理现场反馈并创建普通 Issue | Reporter | 是 |
| `spec-writer` | 生成完整 SPEC 集并创建唯一 Draft MR | Developer | 否 |
| `spec-reviewer` | 独立审查 SPEC | Reporter | 否 |
| `planner` | 生成完整 PLAN 集 | Developer | 否 |
| `plan-reviewer` | 独立审查 PLAN | Reporter | 否 |
| `tasker` | 生成 TASKS 集和稳定 DAG | Developer | 否 |
| `task-reviewer` | 审查 SPEC/PLAN/TASKS 一致性 | Reporter | 否 |
| `coder` | 实现、测试和处理代码返工 | Developer | 否 |
| `tester` | 对精确 MR head 独立测试 | Reporter | 否 |
| `code-reviewer` | 对同一 MR head 独立审查代码 | Reporter | 否 |

Controller 不是 Agent Profile。它使用独立 Maintainer token，独占正式卡创建、GitLab
门禁复核和 `sha=<checked_head>` 合并权限。

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
- `HERMES_DASHBOARD_PORT`：Dashboard 在宿主机发布的端口，默认 `9119`。
- `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` 与至少 32 字节随机
  `HERMES_DASHBOARD_BASIC_AUTH_SECRET`。
- `HOLLYSYS_GITLAB_HOST`、`HOLLYSYS_GITLAB_ALLOWED_GROUPS` 和五类 reviewer/tester
  GitLab identity 白名单。
- 默认 Codex OAuth 不需要填写 `OPENAI_API_KEY`。
- 改用 OpenAI-compatible API 时填写 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`。

Dashboard 通过宿主机 `127.0.0.1:${HERMES_DASHBOARD_PORT:-9119}` 发布。认证值只写入
被忽略且 mode 600 的根 `.env`：

```yaml
HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin
HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=<strong-password>
HERMES_DASHBOARD_BASIC_AUTH_SECRET=<random-at-least-32-bytes>
```

- `SECRET` 是 Dashboard 会话签名密钥，不是登录密码。
- 不把真实凭据写入 Compose、Profile、文档或 Git。

远程访问：

```text
ssh -L 9119:127.0.0.1:9119 <user>@<host>
# 浏览器打开 http://127.0.0.1:9119
```

Hermes Kanban plugin REST 路由不受 Dashboard Basic Auth 保护，因此禁止把 Dashboard
端口改为 LAN/公网发布；Basic Auth 不能替代宿主机 loopback 边界。

部署运行一段时间后需要修改用户名或密码时：

1. 编辑根 `.env` 中的用户名和密码。
2. 同时把 `HERMES_DASHBOARD_BASIC_AUTH_SECRET` 换成新的随机字符串，使此前签发的登录会话立即失效。
3. 重建 Hermes 容器以加载新的环境变量：

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

- `GITLAB_HOST`
- `GITLAB_ALLOWED_GROUPS`
- 独立的 `GITLAB_TOKEN`

只有 `dispatcher`、`prd-writer`、`fde` 填写独立的：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `API_SERVER_PORT`，默认分别为 `8642`、`8643`、`8644`

不要把 Agent 的 GitLab 或 Feishu 凭据放入根 `.env`。Dispatcher 的 token 必须是只读
Reporter；`terminal.home_mode: profile` 使各 Agent 使用独立凭据。

### 3.2.1 Controller Maintainer 凭据

Controller token 不进入环境变量、Profile 或 Git。创建：

```bash
mkdir -p data/controller
install -m 600 /dev/null data/controller/gitlab-token
# 用安全编辑器写入一个作用域受限的 GitLab Maintainer token
```

Controller 启动时拒绝 group/other 可读的 token 文件。该身份只供 `glab api` 和受控
Git workspace 准备使用。五类 identity 白名单可填写 GitLab numeric user id、username
或显示名，用逗号分隔；留空会使对应 gate 必然失败。

### 3.3 lark-cli

三个 Gateway 使用 lark-cli 主动回复。正式 Hollysys 自动交付的 blocked/crash/timeout 首条通知由 Hermes 官方 Kanban notifier 按持久订阅投递，Dispatcher 的后续引导仍回到同一飞书会话；不启动第二个入站消费者。分别复制：

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
字段，再建立订阅和释放卡片。Gateway 的首次自然语言命令解析仍属于模型行为；正式推进不
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

Controller 使用 `glab` 重新验证 GitLab、用 token 不进入 argv/origin 的 Git 凭据助手
幂等准备共享 checkout/worktree。所有 producer 共用该分支、worktree 和 MR；
reviewer/tester 只评论，不修改产物。

### 6.4 确定性推进与单工作卡

固定完整链路：

```text
spec-write → spec-review → plan-write → plan-review
→ tasks-write → tasks-review → implement → test → code-review
→ checked-head merge
```

不创建 continuation、spec/plan/tasks/test/code gate 或 merge 卡。Controller 每两秒
读取各 board 的 append-only `task_events`，每 30 秒执行完整事实对账。阶段由受管
Kanban 卡和 GitLab 实时事实复算；Controller DB 没有 current-stage/head/gate 列。
进程使用文件锁拒绝第二个 Controller；同一后台循环连续失败 5 次会退出，由 Compose
重启。可归属到 run 的对账错误先写入幂等 outbox，避免只留在容器日志。

每次只创建一张下一阶段实质工作卡，幂等键：

```text
<run>:<stage>:<iteration>:work
```

由于 Hermes CLI 没有通用的“创建 todo 后释放”接口，Controller 使用确定性的两阶段
发布：以目标 assignee 和 `initial-status=blocked` 创建控制器 hold，写入受管 ID、建立并
回读验证飞书订阅，再用 `promote` 释放为 ready/todo。这个短暂状态没有
`kanban_block` 事件，不代表人类阻塞。每个外部步骤都有 operation key；进程在步骤前后
崩溃时，卡片 idempotency、订阅 upsert、状态回读和全量对账共同避免重复建卡。

### 6.5 Metadata v3、门禁与重试

- `hollysys_controller.models.CompletionMetadata` 使用 Pydantic `extra=forbid`；
  `scripts/generate_completion_schema.py` 生成仓库 Schema，测试要求两者完全相等。
- 必填 `protocol_version=hollysys-controller/v1`、卡片 iteration 和全部身份字段；
  outcome 只允许 pass/fail/scope_gap/cancelled。scope_gap 必须给目标和非空问题。
- Hermes 官方入口仍保存 free-form metadata。Worker 先自检，Controller 在 done 事件后
  强制验证；非法对象不推进，创建同阶段新 attempt，最多自动重试两次。
- SPEC/PLAN/TASKS gate 会按配置 pattern 枚举审查 commit 上的完整路径集合，重算排序后
  `<path>\0<blob-sha>\n` digest，并核对 MR note 作者、review commit 与 card ID。
- test/code-review note 必须由配置的独立身份发布，并与当前 MR 同一 `head_sha`；任何 push
  使两者失效，从 test 重派。
- fail 返回对应 producer；scope_gap 返回显式目标。SPEC、PLAN、TASKS 分别允许初次后
  最多 3 次返工，implement 相关返工合计最多 5 次。
- 合并前重读 MR ready、pipeline、讨论、所有最新 gate 和当前 head，只调用
  `sha=<checked_head>` merge。SHA 变化只从 test 重派，绝不无 SHA 重试。

### 6.6 Blocked 的原渠道人类闭环

正式 Hollysys 工作卡不使用“创建即自动订阅”。Controller 在释放卡片前显式订阅 run 的原
`chat_id/thread_id`，指定 `notifier_profile=dispatcher` 并从 SQLite 回读验证。Worker
正常完成前退订；crash、timeout 或真正 blocked 时保留订阅。

人类阻塞只用于三类情况：

- `needs_input`：证据互相冲突，必须由发起人作出业务决定。
- `capability`：缺少权限、凭据、环境或只能由人执行的动作。
- `transient`：自动重试不再安全，需要人确认何时再试。

普通依赖使用 Kanban 父子关系，缺陷使用 `fail`/`scope_gap` 返工，不能滥用 blocked。Worker 阻塞前先在卡片写入幂等 `[human-block:v1]` 评论，包含脱敏证据、一个问题/动作、可选答案和恢复校验条件；随后保留订阅并调用 typed `kanban_block`。飞书通知由官方 Gateway notifier 回到原聊天/话题，并在 reason 中使用发起人的 `open_id` 真实 mention：

```text
@发起人 自动交付在 <stage> 暂停：<一个问题或动作>。
请回复本消息并 @dispatcher：
处理阻塞 <run_key> <card-id> <答案/已完成动作>
```

Dispatcher 不能直接恢复。它把真实 sender/chat/thread/message/answer 交给
`hollysysctl resolve`。Controller 验证 root origin、blocked 状态和匹配的
`[human-block:v1]` 评论，以 `block_id + message_id` 幂等创建一张同阶段新 attempt；
新卡订阅完成后，旧 blocked 尝试以严格 v3 `outcome=cancelled` 结束并释放新卡。不会
对同一卡盲目 unblock，也不会创建 continuation/恢复 gate。

部署后必须用真实飞书和 Kanban 做一次受控验收，不能用静态检查代替：

1. 从 A 群 @dispatcher 启动 run，强制一张 worker 卡以 `needs_input` 阻塞。
2. 确认通知只回到 A 群原话题、真实 @ 原发起人，且包含 run/card 和可执行回复格式。
3. 用非发起人、错误话题和不完整答案分别回复，确认不会恢复。
4. 由原发起人给出有效答案，确认形成 block/resolution 评论、旧尝试完成、单张新 retry 卡自动运行。
5. 重放同一回复，确认不创建重复恢复卡；再验证 `capability`、Gateway 重启以及飞书临时发送失败后的恢复。

## 7. 官方镜像零修改的安全边界

本部署不修改官方 Hermes 内核，硬门禁来自 Controller 对自己创建对象的外部控制：

- `dispatcher.kanban.dispatch_in_gateway: true`、`prd-writer/fde: false` 决定哪个 Gateway 自动轮询 Kanban。
- Dispatcher Feishu toolsets 不含 `kanban`；正式图只由 Controller 通过锁定 Hermes CLI 创建。
- Controller 只认 `managed_cards` 中的 ID，并回读核对 `created_by=hollysys-controller`、
  idempotency、parent、assignee、Skill、origin 订阅和严格 card JSON。
- completion schema 由 worker 自检、Controller 强制验证；非法 done 卡不能推进。
- Controller Maintainer token 独占 checked-head merge；Dispatcher 是 Reporter。
- Memory/Skill 修改仍经过 Hermes 官方 write approval。

仍需客观看待的边界：

- 官方 Kanban 仍允许其他 Profile/人类创建非受管卡；Controller 会忽略，但不能阻止其被
  Gateway 调度。生产 board 应限制 Dashboard/主机访问并监控非受管 ready 卡。
- Hermes CLI/Tool 本身不校验 v3；强制发生在 Controller 消费完成事实时。
- bundled/角色 Skill 的 fleet 定制不可变保护。
- Profile 不等于文件系统 sandbox；Developer/Reporter token、protected branch 和独立
  GitLab 身份仍是权限边界。

Dashboard plugin REST 路由绕过 Basic Auth，安全性依赖宿主机 loopback 发布。Controller
不调用 Dashboard REST/WebSocket，只读 SQLite 事件并通过官方 CLI 写入。官方 notifier
负责卡级 blocked/crash/timeout；Controller outbox 负责最终成功、预算耗尽和控制器级
故障，均使用稳定 key 去重。

## 8. 部署、回滚与不迁移约束

备份：

- 完整 `data/`：Kanban DB、Controller DB/token、profiles、sessions、memories、pending、Gateway 和 lark-cli 状态。
- 完整 `projects/`：checkout、共享 worktree 和本地未推送状态。
- 根 `.env` 和各 Profile `.env` 应进入单独的秘密备份，不进入 Git。
- `cli/` 与 `skills/` 是可重建下载物，不需要备份；恢复时运行 `npm ci && npm run assets:install`。

本版本不迁移旧 v2 活动运行：

1. 部署前确认没有 active continuation run；旧历史可保留，Controller 只认
   `managed_cards` 中已登记的卡，不扫描或认领人工仿造的 Controller body。
2. 不做 shadow 双跑。先在测试 board 完成完整 E2E 和故障注入，再在空闲生产 board 启用。
3. 回滚时停止容器，恢复改造前 Compose、`data/` 和 `projects/` 快照。新协议 run 不回灌旧 continuation。
4. v1 只支持单机、单 Controller 和固定完整流程；active-active、快速/标准通道、外部工作流 API、跨项目事务不在范围内。

静态测试与 Compose 解析不是生产 E2E。发布门禁必须额外完成一次真实
PRD→SPEC→PLAN→TASKS→implement→test→code-review→checked-head merge，并验证非法
metadata、代码返工、blocked/resume、重复答复和 Controller 重启。
