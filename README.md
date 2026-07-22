# Hermes Kanban SDD Agent Fleet

这是一套可直接部署到 Ubuntu Linux/AMD64 的 Hermes Agent 0.19.0 多 Agent 交付包。人类通过飞书只启动一次正式交付，之后由 Hermes Kanban 持久调度 `PRD → SPEC → PLAN → TASKS → code → test → review → merge`。

正式交付的最小单位是一个已合入的 PRD 版本：一个 `run_key`、一个共享分支、一个共享 worktree 和一个 Draft MR。正常路径不需要人工逐阶段唤醒；只有需求冲突、权限/凭据/环境故障，或返工预算耗尽时才暂停。

本 README 是总体设计、流程契约、部署和运维的唯一权威说明。

## 1. 总体介绍

### 1.1 系统边界

- **Feishu**：人类命令与通知入口。`dispatcher`、`prd-writer`、`fde` 各使用一个独立飞书 App 和 Gateway。
- **Hermes Kanban**：运行态控制面，保存 card、依赖、attempt、重试、暂停和恢复状态。
- **GitLab**：PRD、SPEC、PLAN、TASKS、代码、MR、pipeline、discussion 和门禁证据的事实源。
- **Git worktree**：同一个 PRD run 内所有 producer 共享一个分支和 worktree。
- **Docker**：单容器运行 12 个 Hermes profiles，使用官方镜像，不构建自定义镜像。

正式 PRD 自动交付不创建 GitLab Task 或 Issue。`TASKS` 仅指仓库中的 SDD 文档。`fde` 仍可独立创建普通 GitLab Issue。

### 1.2 部署架构

Compose 包含两个 service：

| Service | 作用 | 生命周期 |
| --- | --- | --- |
| `tooling-sync` | 按 `config/skills-lock.yaml` 下载并校验 glab、lark-cli 和官方 Skills | 启动前运行一次，成功后退出 |
| `hermes` | 运行 Hermes、12 个 profiles、3 个 Gateway 和 Kanban | 常驻，`restart: unless-stopped` |

两个 service 都直接使用宿主机已有的：

```text
nousresearch/hermes-agent:v2026.7.20
```

Compose 配置了 `pull_policy: never`，不会构建镜像，也不会静默拉取其他 tag。profiles、模板、schema、脚本、锁文件和 Hermes 0.19.0 小型运行时补丁均以只读 volume 挂载。glab、lark-cli 和外部 Skills 位于独立 named volume，更新它们不需要重建 Hermes 镜像。

主要持久目录：

| 宿主机变量 | 容器路径 | 内容 |
| --- | --- | --- |
| `HERMES_DATA_DIR` | `/opt/data` | profiles、独立 `.env`、Kanban SQLite、sessions、memory、Gateway 和 lark-cli 状态 |
| `PROJECTS_DIR` | `/workspace/projects` | 动态 clone 的项目、共享 checkout 和 run worktree |
| `TOOLING_VOLUME_NAME` | `/opt/fleet/vendor` | 锁定版本的 CLI 与 Skills |

不要让两个正在运行的容器共享同一个 `HERMES_DATA_DIR` 或 `PROJECTS_DIR`。Hermes 官方 profile 隔离和 Docker/s6 行为参见 [Profiles 文档](https://hermes-agent.nousresearch.com/docs/user-guide/profiles/)与 [Docker 文档](https://hermes-agent.nousresearch.com/docs/user-guide/docker/)。

### 1.3 Agent 职责与权限

| Profile | 主要职责 | GitLab 角色 | Git 能力 | 飞书 Gateway |
| --- | --- | --- | --- | --- |
| `dispatcher` | 校验启动输入、管理 Kanban、核对门禁、checked-head merge、通知人类 | Maintainer | clone、评论、合并；不编写专业产物 | 是，正式交付入口 |
| `prd-writer` | 与人类编写 PRD，创建 PRD 分支和 MR | Developer | clone、push、创建 MR | 是，PRD 协作入口 |
| `fde` | 整理现场反馈、脱敏、创建普通 Issue | Reporter | clone 只读、Issue API | 是，现场反馈入口 |
| `spec-writer` | 生成完整 SPEC 集并创建唯一 Draft MR | Developer | clone、push、创建/更新 MR | 否 |
| `spec-reviewer` | 独立审查完整 SPEC 集 | Reporter | clone 只读、MR 评论 | 否 |
| `planner` | 根据通过的 SPEC 和真实仓库生成完整 PLAN 集 | Developer | clone、push | 否 |
| `plan-reviewer` | 独立审查完整 PLAN 集 | Reporter | clone 只读、MR 评论 | 否 |
| `tasker` | 生成完整 TASKS 集和稳定任务 DAG | Developer | clone、push | 否 |
| `task-reviewer` | 独立审查 SPEC/PLAN/TASKS 一致性 | Reporter | clone 只读、MR 评论 | 否 |
| `coder` | 实现完整 TASKS、编写测试、处理代码返工 | Developer | clone、push、将 MR 标为 ready | 否 |
| `tester` | 对精确 MR head 独立测试 | Reporter | clone 只读、MR 评论 | 否 |
| `code-reviewer` | 对已测试的同一 MR head 独立代码审查 | Reporter | clone 只读、MR 评论 | 否 |

只有 `dispatcher` 可以创建或链接下一张 Kanban card。运行时补丁在 handler 层拒绝其他 profile 调用 `kanban_create`/`kanban_link`，dispatcher 取下一卡时还会校验 `created_by=dispatcher`。

### 1.4 凭据隔离

根 `.env` 只保存 Compose、模型和 Git commit identity 等共享部署配置。GitLab 和飞书秘密只存在：

```text
${HERMES_DATA_DIR}/profiles/<profile>/.env
```

隔离规则：

- 12 个 profile 各使用不同的 `GITLAB_TOKEN`。
- 只有 `dispatcher`、`prd-writer`、`fde` 存在 `FEISHU_*` 和 `LARKSUITE_CLI_*` 变量。
- 三个飞书 App ID 和 App Secret 分别独立，不能复用。
- profile 目录权限必须为 `0700`，`.env` 必须为普通文件、权限 `0600`、所有者必须与容器 Hermes 用户一致，禁止软链接。
- `terminal.home_mode: profile` 让 glab、git 等外部 CLI 使用独立 HOME。
- Git remote 始终使用无 token 的 HTTPS URL，不执行持久化 token 的 `glab auth login`。
- lark-cli 的加密状态分别存放在 `/opt/data/profiles/<profile>/.lark-cli/`，三个 Gateway 之间不共享。
- 校验日志只显示变量名、profile 和摘要比较结果，不打印原始秘密。

## 2. 流程设计

### 2.1 人类启动协议

向 `dispatcher` 的飞书机器人发送：

```text
实现 PRD <精确 PRD blob/raw URL> <已合并 PRD MR URL>
```

示例中的 PRD URL 必须使用完整 40 位 commit，而不是 `main`、短 SHA 或可变化的 branch URL：

```text
实现 PRD https://gitlab.example.com/group/project/-/blob/<40位merge-sha>/docs/prds/prd-login.md https://gitlab.example.com/group/project/-/merge_requests/123
```

缺少任一 URL、URL 不属于同一 host/项目、PRD 路径不明确或仓库不明确时，dispatcher 只在原渠道继续询问，不创建 run、分支、worktree 或 MR。

### 2.2 启动校验顺序

dispatcher 必须按顺序读取 GitLab live state：

1. URL 必须是无内嵌凭据的 HTTPS GitLab URL。
2. host 必须等于该 profile 的 `GITLAB_HOST`，项目必须位于 `GITLAB_ALLOWED_GROUPS` 允许的群组下。
3. 查询项目。项目不存在和无权限无法可靠区分时，统一回复“项目不存在或当前身份无访问权限”，不能误报“文件不存在”。
4. 项目存在后查询 PRD 文件；此时缺失才回复“文件不存在”。
5. `archived=true` 时立即停止，并回复“项目已归档，不允许修改”。
6. PRD MR 必须是 `merged`，目标必须是项目当前 `default_branch`。
7. PRD URL 的 commit 必须是该 MR 的有效 merge commit，且 MR 变更包含指定 PRD。
8. 默认分支当前 PRD blob 必须仍等于请求版本；路径已有更新时拒绝旧版本。

项目描述中的中文项目名只用于展示。所有定位、board 和恢复逻辑使用不可变的 `project_id` 与 `path_with_namespace`。

### 2.3 Run、checkout、分支和 MR

`run_key` 由以下值的规范化组合计算：

```text
host + project_id + prd_path + prd_commit_sha
```

同一版本重复收到飞书消息时：

- 已存在 active/paused/blocked run：恢复或返回当前状态，不创建重复对象。
- 对应交付 MR 已 merged：返回已完成结果和链接。
- 同一路径的新 PRD merge commit：生成新的 `run_key`，允许启动新 run。

dispatcher 通过 HTTPS 动态准备：

```text
checkout: /workspace/projects/p<project_id>-<repo-slug>
board:    gitlab-p<project_id>
worktree: /workspace/projects/worktrees/p<project_id>/<run_key>
```

默认分支名为：

```text
feature/<prd-basename>-<prd_sha8>
```

若目标仓库已有明确分支规范，优先服从仓库规范。所有 Agent card 必须携带项目 ID/路径/中文显示名、checkout、worktree、分支、目标分支、PRD 路径与 commit、PRD MR、`run_key`、交付 MR 和期望 head。

`spec-writer` 在首个有效 SPEC commit 后创建：

```text
Draft: [PRD] <prd-basename>.md
```

后续所有 producer 和返工都复用同一分支、worktree 和 MR。`coder` 完成完整实现与自测后才将 MR 标记为 ready。

### 2.4 串行阶段

```text
PRD 已合入
  → SPEC 全集
  → SPEC review
  → PLAN 全集
  → PLAN review
  → TASKS 全集
  → TASK review
  → code + self-test
  → tester
  → code-reviewer
  → checked-head merge
  → Feishu 完成通知
```

每一阶段整批过门禁，不按单个 SPEC 拆分分支或 MR。reviewer/tester 只写同一 MR 的评论和 gate，不修改 producer 工件。

### 2.5 仓库工件目录

不迁移或改名仓库既有文档。新产物固定为：

```text
docs/prds/prd-xxx-yyy.md
docs/prds/prd-xxx-yyy/specs/spec-<key>.md
docs/prds/prd-xxx-yyy/plans/plan-<key>.md
docs/prds/prd-xxx-yyy/tasks/task-<key>.md
```

`spec-<key>.md`、`plan-<key>.md`、`task-<key>.md` 的 key 集合必须完整且一一对应。所有新增文件和目录使用小写英文语义词，多个单词用 `-` 连接；既有 `.NET service` 命名保持仓库原约定。

### 2.6 门禁与失效

设计工件门禁绑定：

```text
artifact_digest = sha256(按路径排序后的 "path\0blob_sha\n")
review_commit_sha = reviewer 实际读取工件时的 commit
```

后续增加下游文件不会误使上游门禁失效；已通过的工件被修改时按以下规则作废：

| 变更 | 作废门禁 |
| --- | --- |
| SPEC | SPEC、PLAN、TASKS、tester、code-reviewer |
| PLAN | PLAN、TASKS、tester、code-reviewer |
| TASKS | TASKS、tester、code-reviewer |
| 代码或测试 | tester、code-reviewer |

tester 与 code-reviewer 必须绑定同一个当前 MR `head_sha`。任何新 push 都同时作废二者结论，必须重新测试和代码审查。

### 2.7 返工与人工暂停

- SPEC、PLAN、TASKS 各阶段最多返工 3 轮。
- 代码、测试、代码审查引起的代码返工合计最多 5 轮。
- reviewer 必须区分当前产物问题与上游 `scope_gap`，scope gap 返回真正的上游 owner。
- 需求冲突、合同外业务决策、能力/凭据/环境故障或预算耗尽时进入暂停，并在飞书说明原因、证据、已尝试动作和需要的人类决定。

### 2.8 Checked-head merge

dispatcher 只在以下条件同时成立时合并：

- MR 已 ready 且 GitLab 报告可合并。
- 必需 pipeline 状态为 `success`。
- 所有阻塞 discussion 已解决。
- SPEC、PLAN、TASKS artifact gate 仍有效。
- tester 与 code-reviewer 均对当前同一个 `head_sha` 给出 pass。
- 当前 head 等于 dispatcher 记录的 `checked_head`。

合并 API 必须携带：

```text
sha=<checked_head>
```

合并成功后，dispatcher 使用 merge commit SHA 在 MR 留下 PRD、SPEC、PLAN、TASKS 的排序后永久 blob 链接，清理成功 run 的 worktree，并在原飞书群聊、话题或单聊回复。群聊和话题中必须 `@` 原发起人。

## 3. 部署说明

### 3.1 目标环境与资源

目标环境：

- Ubuntu Linux，AMD64/x86_64。
- Docker Engine 已安装并运行。
- Docker Compose v2，可使用 `docker compose`。
- 宿主机已经存在 `nousresearch/hermes-agent:v2026.7.20`。
- 建议至少 2 vCPU、4 GB RAM；磁盘需要覆盖 Hermes 数据、tooling 和所有目标项目/build 产物。
- 部署用户能读写本仓库、`HERMES_DATA_DIR` 和 `PROJECTS_DIR`，且能访问 Docker daemon。

网络要求：

- 始终需要访问私有 GitLab、飞书和所选模型服务。
- 首次或更新 tooling 时，容器需要访问 `gitlab.com`、`github.com` 和 npm registry。
- 如果生产环境不能访问公网，应在可联网环境预填充同名 tooling volume，再以受控方式迁移；不能跳过版本和 checksum 校验。

检查环境：

```bash
uname -s
uname -m
docker version
docker compose version
docker image inspect nousresearch/hermes-agent:v2026.7.20 >/dev/null
```

期望 `uname -s` 为 `Linux`，`uname -m` 为 `x86_64`。

### 3.2 获取部署包

将本仓库 clone 到 Ubuntu 的固定目录，然后进入仓库根目录。不要只复制单个 Compose 文件；启动依赖本仓库中的 profiles、模板、schema、补丁、锁文件和脚本。

```bash
git clone <本部署包仓库URL> hermes-agents-by-kanban
cd hermes-agents-by-kanban
git status --short
```

全新 clone 的 `git status --short` 应为空。

### 3.3 配置根 `.env`

```bash
cp .env.example .env
id -u
id -g
```

将 `.env` 的 `PUID`、`PGID` 改为上述输出。使用该 UID 对应的宿主用户执行后续初始化，不要用 `sudo` 创建 profile 凭据文件。

至少配置：

```dotenv
HERMES_IMAGE=nousresearch/hermes-agent:v2026.7.20
HERMES_DATA_DIR=./.runtime/hermes
PROJECTS_DIR=./.runtime/projects
PUID=<id -u 输出>
PGID=<id -g 输出>

FLEET_MODEL=<Hermes 支持的 provider/model>
<与所选模型对应的 API_KEY>=<模型服务密钥>

GIT_COMMIT_NAME_PRD_WRITER="Hermes PRD Writer"
GIT_COMMIT_EMAIL_PRD_WRITER=prd-writer@example.com
GIT_COMMIT_NAME_SPEC_WRITER="Hermes SPEC Writer"
GIT_COMMIT_EMAIL_SPEC_WRITER=spec-writer@example.com
GIT_COMMIT_NAME_PLANNER="Hermes Planner"
GIT_COMMIT_EMAIL_PLANNER=planner@example.com
GIT_COMMIT_NAME_TASKER="Hermes Tasker"
GIT_COMMIT_EMAIL_TASKER=tasker@example.com
GIT_COMMIT_NAME_CODER="Hermes Coder"
GIT_COMMIT_EMAIL_CODER=coder@example.com
```

只填写所用模型对应的 Key。GitLab、飞书秘密不得写入根 `.env`。

### 3.4 创建 GitLab 身份

建议在包含所有目标项目的允许群组创建 12 个独立 group access token。每个 token 使用不同名称和到期时间，记录轮换责任人。GitLab 官方角色说明见[权限表](https://docs.gitlab.com/user/permissions/)，scope 含义见 [Access token scopes](https://docs.gitlab.com/security/tokens/access_token_scopes/)。

| Profiles | 角色 | Scopes | 用途 |
| --- | --- | --- | --- |
| `dispatcher` | Maintainer | `api`, `write_repository` | 查询全部门禁、评论、checked-head merge |
| `prd-writer`, `spec-writer`, `planner`, `tasker`, `coder` | Developer | `api`, `write_repository` | HTTPS clone/push、创建或更新 MR |
| `fde`, `spec-reviewer`, `plan-reviewer`, `task-reviewer`, `tester`, `code-reviewer` | Reporter | `api`, `read_repository` | HTTPS clone、Issue/MR API 与评论，不 push |

注意：

- `api` 用于 GitLab API，不应假设它自动提供 Git-over-HTTPS push/pull；按表同时配置 repository scope。
- 12 个 token 必须非空且互不相同，不能用同一 Maintainer token 复制 12 份。
- 目标默认分支必须 protected，并允许 dispatcher 的 Maintainer 身份合并。
- reviewer/tester/FDE 不应获得 push 权限。
- `GITLAB_ALLOWED_GROUPS` 使用逗号分隔的完整群组路径，例如 `mom,industrial/mes`，不写 URL。

### 3.5 创建三个飞书 App

分别为 `dispatcher`、`prd-writer`、`fde` 创建三个企业自建应用，不复用 App ID 或 App Secret。

每个应用：

1. 启用机器人能力。
2. 配置至少以下权限：`im:message`、`im:message:send_as_bot`、`im:resource`、`im:chat`、`im:chat:readonly`。
3. 按需要增加 `im:message.reactions:readonly`、`admin:app.info:readonly`、`contact:user.id:readonly`。
4. 订阅事件 `im.message.receive_v1`。
5. 使用长连接/WebSocket 接收事件，不要求公网 webhook。
6. 创建并发布应用版本；仅保存配置但未发布时，机器人可能无法收发消息。
7. 将机器人加入允许的群聊，记录发起人的 open ID 和默认 chat ID。

Hermes 对飞书的配置说明见[飞书 Gateway 文档](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/feishu)。

### 3.6 初始化 12 份 profile `.env`

```bash
./scripts/init-profile-envs.sh
```

脚本读取根 `.env` 的 `HERMES_DATA_DIR`，只创建不存在的文件。重复执行不会覆盖已有凭据。

worker profile 示例：

```dotenv
HERMES_PROFILE=spec-reviewer
GITLAB_HOST=green-git.hollysys.net
GITLAB_ALLOWED_GROUPS=mom,industrial/mes
GITLAB_TOKEN=<该 profile 的独立 token>
```

dispatcher Gateway 示例：

```dotenv
HERMES_PROFILE=dispatcher
GITLAB_HOST=green-git.hollysys.net
GITLAB_ALLOWED_GROUPS=mom,industrial/mes
GITLAB_TOKEN=<dispatcher 独立 token>
API_SERVER_PORT=8642
FEISHU_APP_ID=<dispatcher App ID>
FEISHU_APP_SECRET=<dispatcher App Secret>
FEISHU_DOMAIN=feishu
FEISHU_CONNECTION_MODE=websocket
FEISHU_ALLOWED_USERS=<允许发起人的 open_id>
FEISHU_HOME_CHANNEL=<默认 chat_id>
FEISHU_GROUP_POLICY=allowlist
FEISHU_REQUIRE_MENTION=true
LARKSUITE_CLI_CONFIG_DIR=/opt/data/profiles/dispatcher/.lark-cli/config
LARKSUITE_CLI_DATA_DIR=/opt/data/profiles/dispatcher/.lark-cli/data
LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1
LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1
```

`prd-writer`、`fde` 使用各自模板中预设的端口和 lark-cli 路径，不要复制 dispatcher 路径。九个 worker 的 `.env` 中不得出现任何 `API_SERVER_PORT`、`FEISHU_*` 或 `LARKSUITE_CLI_*`。

检查权限：

```bash
find "${HERMES_DATA_DIR:-./.runtime/hermes}/profiles" -maxdepth 1 -mindepth 1 -type d -exec stat -c '%a %U:%G %n' {} \;
find "${HERMES_DATA_DIR:-./.runtime/hermes}/profiles" -maxdepth 2 -name .env -type f -exec stat -c '%a %U:%G %n' {} \;
```

期望 profile 目录为 `700`、`.env` 为 `600`，且所有者 UID 等于根 `.env` 中的 `PUID`。

### 3.7 静态校验并启动

```bash
./scripts/verify-bundle.sh
docker compose up -d
```

首次启动顺序：

1. `tooling-sync` 下载并验证锁定工具，写入 named volume 后退出 0。
2. `hermes` 启动，确认版本精确为 0.19.0 并幂等应用 Kanban guard patch。
3. bootstrap 安装 12 个 profiles，验证 12 个 GitLab 身份和 3 个飞书身份。
4. 为五个 producer 配置独立 Git commit identity。
5. 将三个飞书 `.env` 绑定到各自 lark-cli 加密存储。
6. s6 启动 dispatcher、prd-writer、fde 三个 Gateway。

若 `tooling-sync` 失败，Hermes 不会启动。先修复网络或锁文件问题，不要绕过 checksum。

### 3.8 一键运行态验收

容器启动后执行：

```bash
./scripts/verify-runtime.sh
```

该命令只读取 Docker、Hermes、GitLab 和本地加密配置状态，不创建 Issue、分支、MR，也不发送飞书消息。成功时应报告：

- Linux AMD64、本地镜像和 Compose service 正常。
- Hermes 0.19.0、Kanban guard、锁定 tooling 均正确。
- 12 个 profiles 存在，三个 Gateway running，九个 worker 未启动 Gateway。
- 12 个 GitLab API 身份不同且满足角色要求。
- 三个 lark-cli 状态目录独立且权限正确。

失败时按输出中的 profile 和检查项处理；脚本不会打印 token 或 App Secret。

### 3.9 首次消息 smoke test

运行态验收通过后，分别验证三个机器人原渠道回复：

1. 向 `prd-writer` 发送一条无敏感信息的 PRD 澄清请求，确认单聊/群聊/话题回复正确。
2. 向 `fde` 发送一条测试现场反馈，明确要求只分析、不创建 Issue，确认分类与回复渠道。
3. 向 `dispatcher` 发送缺少 URL 的 `实现 PRD`，确认它继续询问且没有创建 run。
4. 最后使用专用测试项目和已合入测试 PRD 执行完整启动协议。

前三项验证 Gateway 和原渠道回复，不证明 GitLab 写权限或完整自治。完整测试项目应继续验证 branch/MR/pipeline/discussion、checked-head merge、重启恢复和故障注入。

## 4. 日常运维

### 4.1 状态与日志

```bash
docker compose ps
docker compose logs tooling-sync
docker compose logs --tail=200 hermes
docker compose exec hermes hermes --version
docker compose exec hermes hermes profile list
docker compose exec hermes hermes gateway list
docker compose exec hermes hermes -p dispatcher kanban boards list
```

观察项目 board：

```bash
docker compose exec hermes hermes -p dispatcher kanban --board gitlab-p<project-id> list
docker compose exec hermes hermes -p dispatcher kanban --board gitlab-p<project-id> watch
```

### 4.2 启停与重启

```bash
docker compose stop
docker compose start
docker compose restart hermes
docker compose down
```

`docker compose down` 不删除 `HERMES_DATA_DIR`、`PROJECTS_DIR` 或 named tooling volume。

### 4.3 轮换单个 profile 凭据

1. 修改 `${HERMES_DATA_DIR}/profiles/<profile>/.env`。
2. 保持目录 `0700`、文件 `0600`、所有者 UID 正确。
3. 执行 `docker compose restart hermes`。
4. 再执行 `./scripts/verify-runtime.sh`。

bootstrap 会重新校验 12 个 token；飞书 profile 会从权威 `.env` 重新绑定自己的 lark-cli 加密缓存。由于是单容器，三个 Gateway 会一起重启，但其他 profile 的凭据文件不会被改写。

### 4.4 更新 glab、lark-cli 或 Skills

先更新 `config/skills-lock.yaml` 中的精确版本/revision，再执行：

```bash
docker compose stop hermes
docker compose run --rm tooling-sync
docker compose up -d hermes
./scripts/verify-runtime.sh
```

tooling 使用不可变 release 目录并原子切换 `current` 链接。不要在 Hermes 正在运行时切换 tooling 版本。

### 4.5 升级 Hermes

1. 先把目标官方镜像放入宿主机。
2. 同步修改根 `.env`、Compose 默认 tag、`skills-lock.yaml` 中的 Hermes release/revision、profiles 最低版本和运行时补丁。
3. 在隔离环境验证新版本补丁仍可应用。
4. 停止生产容器、备份数据后升级。

运行时补丁会拒绝非 0.19.0 版本，因此不能只修改镜像 tag。默认不会覆盖持久数据中的 `config.yaml`；确需接受仓库托管配置时，将 `FLEET_FORCE_CONFIG=1` 启动一次，确认后立即恢复为 `0`。

### 4.6 备份与恢复

备份前停止 Hermes：

```bash
docker compose stop hermes
```

完整备份：

- 根 `.env`。
- 整个 `HERMES_DATA_DIR`，包括 Kanban SQLite、profiles、sessions 和 lark-cli 状态。
- 整个 `PROJECTS_DIR`，或确认所有未合并分支已安全 push 后再从 GitLab重建。
- `config/skills-lock.yaml` 对应的 tooling volume，或保证恢复环境可以重新联网同步。

恢复时必须保持文件所有者、`0700`/`0600` 权限和 Compose 项目/volume 名称一致。恢复后先执行静态校验，再启动并运行 `verify-runtime.sh`。

### 4.7 删除 tooling volume

```bash
docker compose down -v
```

该命令删除 named tooling volume，下次启动需要重新联网下载 CLI 和 Skills。它不会自动删除 bind-mounted 的 `HERMES_DATA_DIR` 或 `PROJECTS_DIR`。删除实际运行数据前必须另行备份并确认目标路径。

## 5. 常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `No such image` | 官方 tag 不在本机，且 `pull_policy: never` | 手工准备 `nousresearch/hermes-agent:v2026.7.20` 后重试 |
| `tooling-sync` 下载失败 | 无法访问 GitHub、GitLab.com、npm 或 TLS/代理错误 | 修复容器网络/CA/代理；不要绕过 checksum |
| profile `.env` owner/mode 失败 | 用 sudo 初始化、PUID 不匹配、文件为软链接 | 用部署 UID 修正所有者，目录设 `0700`、文件设 `0600`，替换软链接为普通文件 |
| token/app 重复 | 多个 profile 复制了同一秘密 | 为每个 profile/App 创建独立凭据，修改后重启 |
| 根 `.env` 缺 `FLEET_MODEL` 或 commit identity | 共享部署参数未填 | 填写根 `.env`，不要把它们移入 profile `.env` |
| Gateway stopped/failed | 飞书 App 未发布、权限/事件缺失、App Secret 错误或端口冲突 | 检查三个 App、事件和 profile 日志，再重启 Hermes |
| GitLab 401 | token 错误、过期、被撤销或 host 不匹配 | 轮换对应 profile token，确认 `GITLAB_HOST` 不含路径 |
| GitLab 403 | 角色/scope 不足、protected branch 规则拒绝 | 按权限表修正该身份，不临时共享 dispatcher token |
| “项目不存在或当前身份无访问权限” | 路径错误或 token 看不到项目 | 由管理员核对项目 ID、群组成员和 token scope |
| “文件不存在” | 项目可访问，但 PRD 路径不存在 | 使用 GitLab 中精确的 `docs/prds/prd-*.md` blob/raw URL |
| “项目已归档” | `archived=true` | 不继续交付；由项目管理员决定是否恢复项目 |
| MR pipeline/discussion 阻塞 | pipeline 未成功或阻塞 discussion 未解决 | 修复失败项/解决讨论，dispatcher 再次核对 live state |
| `head drift` | tester/reviewer 通过后又有 push | 对新 head 重新执行 tester 与 code-reviewer，不能复用旧结论 |
| 容器重启后 run 未推进 | Kanban card blocked/paused、凭据故障或 worker attempt 耗尽 | 查看 board/card 和 profile 日志，修复原因后通过 dispatcher 恢复 |

## 6. 验证声明边界

```bash
./scripts/verify-bundle.sh
```

验证仓库结构、Hermes 0.19 锁、profile 契约、凭据模板、schema、模板、脚本、合同测试和 Compose 配置。

```bash
./scripts/verify-runtime.sh
```

在 Ubuntu 启动后验证本机镜像、容器、Hermes/CLI 版本、profiles、Gateway、凭据隔离和 GitLab 只读身份。

两者通过可声明“静态部署包及本机只读运行验收通过”。真实 GitLab 写入、三个飞书机器人端到端消息、protected branch、MR/pipeline/discussion、checked-head merge、崩溃恢复和故障注入全部通过前，不得声明“生产自治 E2E”。
