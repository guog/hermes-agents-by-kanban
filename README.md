# Hermes Kanban SDD Agent Fleet

这是一套面向 Ubuntu Linux/AMD64 的 Hermes Agent 0.19.0 多 Agent 部署包。部署只运行官方 Hermes 镜像，通过 `docker-compose.yaml` 挂载本地目录；不构建定制镜像，不修改 `/opt/hermes`，不执行 fleet 初始化、补丁、同步或验证脚本。

人类通过飞书启动一次正式交付，之后由 Hermes Kanban 持久调度：

```text
PRD → SPEC → SPEC review → PLAN → PLAN review → TASKS → TASK review
    → code + self-test → test → code review → checked-head merge
```

正式交付采用一个 `run_key`、一个共享分支、一个共享 worktree 和一个 MR。GitLab 保存交付工件与门禁证据，Kanban 保存 card、依赖、attempt、重试和恢复状态，Feishu 负责命令与通知。

## 1. 部署结构

Compose 只有一个 `hermes` service：

- 使用固定 digest 的官方 `nousresearch/hermes-agent:v2026.7.20`。
- 以 `sleep infinity` 保持容器常驻，Gateway 和 Dashboard 由官方镜像内置 s6 监督。
- Dashboard 发布到宿主机所有网卡，并使用 Hermes 内置的用户名/密码认证。
- 首次缺少镜像时由 Compose 自动拉取，不执行构建。
- 不覆盖镜像 entrypoint，不向 `/opt/hermes` 或 `/etc/cont-init.d` 挂载文件。

宿主机目录：

```text
.
├── docker-compose.yaml
├── .env.example
├── data/                       # Hermes 的完整可写 /opt/data
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
├── tooling/                    # 预置的 Linux AMD64 CLI 与官方 Skills
├── templates/                  # MR、评论、Kanban card 模板
└── schemas/                    # Agent 自检用 completion metadata schema
```

挂载关系：

| 宿主机 | 容器 | 模式 |
| --- | --- | --- |
| `${HERMES_DATA_DIR:-./data}` | `/opt/data` | 可写 |
| `${PROJECTS_DIR:-./projects}` | `/workspace/projects` | 可写 |
| `./tooling` | `/opt/tooling` | 只读 |
| `./templates` | `/opt/fleet/templates` | 只读 |
| `./schemas` | `/opt/fleet/schemas` | 只读 |

不要让两个运行中的容器共享同一个 `HERMES_DATA_DIR` 或 `PROJECTS_DIR`。

## 2. 预置 Agent

| Profile | 职责 | GitLab 权限建议 | Feishu Gateway |
| --- | --- | --- | --- |
| `dispatcher` | 校验启动输入、管理 Kanban、门禁、checked-head merge、通知 | Maintainer | 是 |
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
- 使用 API key 模型时需要的 provider key。

Dashboard 通过 `0.0.0.0:${HERMES_DASHBOARD_PORT:-9119}` 发布。首次部署前，直接编辑
`docker-compose.yaml` 中以下三个明文值：

```yaml
HERMES_DASHBOARD_BASIC_AUTH_USERNAME: "admin"
HERMES_DASHBOARD_BASIC_AUTH_PASSWORD: "CHANGE_ME_BEFORE_DEPLOYMENT"
HERMES_DASHBOARD_BASIC_AUTH_SECRET: "CHANGE_ME_TO_A_RANDOM_SECRET_OF_AT_LEAST_32_BYTES"
```

- 将用户名和密码替换为实际值。
- 将 `SECRET` 替换为至少 32 字节的随机字符串；它不是登录密码，而是 Dashboard 会话签名密钥。
- `docker-compose.yaml` 因此包含明文凭据，只能由部署管理员读取，不要提交实际密码到 Git。

局域网设备访问：

```text
http://<宿主机局域网 IP>:9119
```

Dashboard 登录后可以读取或修改 Hermes 配置与凭据，并触发 Agent 操作。即使已有密码，也只允许受信任的局域网访问，并禁止把该 HTTP 端口映射到公网。

部署运行一段时间后需要修改用户名或密码时：

1. 编辑 `docker-compose.yaml` 中的 `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` 和
   `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD`。
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

不要把 GitLab 或 Feishu 凭据放入根 `.env`。`terminal.home_mode: profile` 使 git/glab/lark 等外部工具使用各 Profile 独立的 `home/`。

### 3.3 lark-cli

三个 Gateway 使用 lark-cli 主动回复和发送完成/暂停通知。分别复制：

```bash
cp \
  data/profiles/dispatcher/.lark-cli/config/hermes/config.json.example \
  data/profiles/dispatcher/.lark-cli/config/hermes/config.json
chmod 600 data/profiles/dispatcher/.lark-cli/config/hermes/config.json
```

将其中的 `appId`、`appSecret` 替换为该 Profile `.env` 中同一组 Feishu 凭据。配置固定 `defaultAs=bot`、`strictMode=bot`，不允许使用用户身份冒充人类。

`prd-writer` 和 `fde` 做同样处理。实际 `config.json` 已被 Git 忽略。

### 3.4 模型和 Git identity

按需要直接编辑每个 Profile 的 `config.yaml`：

```yaml
model: "provider/model"
```

producer 的提交身份位于：

```text
data/profiles/{prd-writer,spec-writer,planner,tasker,coder}/home/.gitconfig
```

默认 `.invalid` 邮箱明确标识自动化提交。如果 GitLab 要求已验证提交邮箱，部署前换成管理员创建的已验证 bot alias。

## 4. 启动与运维

启动：

```bash
docker compose up -d
```

常用原生命令：

```bash
docker compose ps
docker compose logs --tail=200 hermes
docker compose exec hermes hermes version
docker compose exec hermes hermes profile list
docker compose exec hermes hermes gateway list
docker compose exec hermes hermes -p dispatcher kanban boards list
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

## 5. 锁定工具

部署包预置：

| 工具 | 版本 |
| --- | --- |
| `glab` | 1.108.0 Linux AMD64 |
| `lark-cli` | 1.0.72 Linux AMD64 |
| GitLab `glab` Skill | revision `933cee89...` |
| Lark `lark-shared`、`lark-im` Skills | revision `d6cebd67...` |

来源和 SHA-256 记录在 `tooling/manifest.yaml`，随附许可证位于 `tooling/licenses/`。目标机不下载、不安装、不同步工具。升级工具时由维护者在发布包中整体替换并人工审查 manifest；运行中的 Agent 只读挂载 `tooling/`。

## 6. 自动开发工作流

### 6.1 启动协议

向 `dispatcher` 发送：

```text
实现 PRD <精确 PRD blob/raw URL> <已合并 PRD MR URL>
```

PRD URL 必须固定到完整 commit SHA。Dispatcher 依次验证 GitLab host/group、项目可读、未归档、PRD 存在、PRD MR 已合入当前默认分支、MR 包含该 PRD、默认分支仍是请求版本。

`run_key` 由下列身份确定：

```text
host + project_id + prd_path + prd_commit_sha
```

重复消息恢复现有 run；已完成则返回结果；同一路径的新 PRD commit 创建新 run。

### 6.2 共享 checkout、分支和 MR

路径固定为：

```text
checkout: /workspace/projects/p<project_id>-<repo-slug>
worktree: /workspace/projects/worktrees/p<project_id>/<run_key>
board:    gitlab-p<project_id>
```

默认分支名：

```text
feature/<prd-basename>-<prd_sha8>
```

Dispatcher Skill 使用标准 `git`、`glab` 和 `hermes kanban` 命令幂等准备与清理 worktree，不调用 fleet 脚本。所有 producer 共用该分支、worktree 和 MR；reviewer/tester 只评论，不修改产物。

### 6.3 Worker/continuation 双卡

每个 dispatcher gate 创建：

```text
dispatcher gate G
  ├─ worker W
  └─ dispatcher continuation C，parent=W
```

稳定 idempotency key：

```text
<run>:<stage>:<iteration>:work
<run>:<stage>:<iteration>:continue
```

G 只有在 W、C 和依赖都核对完成后才能结束。W 完成后 C 重新读取 completion metadata 及 GitLab live state，再创建下一对卡。任何未知 `head_sha`、digest、review、pipeline 或 merge 结论在 live reconcile 前保持为空。

### 6.4 门禁

- SPEC、PLAN、TASKS 分别使用排序后的 `path + blob_sha` digest 和 reviewer 实际读取的 commit。
- 修改上游产物会作废对应门禁及全部下游门禁。
- tester 与 code-reviewer 必须绑定同一个当前 MR `head_sha`；任何 push 都使两者失效。
- SPEC、PLAN、TASKS 各最多返工 3 轮；代码相关返工合计最多 5 轮。
- Merge 必须使用 GitLab checked-head 参数 `sha=<checked_head>`。
- completion metadata 按 `schemas/card-completion.schema.json` 自检；卡片正文不能代替顶层字段。

## 7. 官方镜像零修改的安全边界

本部署保留业务流程，但不再包含原来的 Hermes 运行时补丁，因此必须客观区分配置约束和硬门禁：

- `dispatcher.kanban.dispatch_in_gateway: true`、`prd-writer/fde: false` 决定哪个 Gateway 自动轮询 Kanban。
- SOUL 和 Skill 规定只有 dispatcher 创建/链接正式卡，并要求 continuation 检查 `created_by=dispatcher`。
- completion schema 由 worker 和 dispatcher 自检。
- Memory/Skill 修改仍经过 Hermes 官方 write approval。

以下内容不再由镜像 handler 或数据库层强制：

- 非 dispatcher 直接调用 `kanban_create`/`kanban_link` 的拒绝。
- dispatcher 只调度 `created_by=dispatcher` 卡片的 SQL 过滤。
- CLI、Dashboard、Tool 对 completion metadata 的统一强制校验。
- bundled/角色 Skill 的 fleet 定制不可变保护。

因此，这是一套“官方镜像 + 配置/Skill 治理”的自动化流程，不应再宣称具备定制补丁提供的防恶意 Profile 硬隔离。Profile 不等于文件系统 sandbox；令牌最小权限、独立凭据、人工审批和 GitLab protected branch 仍是必要边界。

## 8. 备份与旧部署迁移

备份：

- 完整 `data/`：Kanban DB、profiles、sessions、memories、skills、pending approval、Gateway 和 lark-cli 状态。
- 完整 `projects/`：checkout、共享 worktree 和本地未推送状态。
- 根 `.env` 和各 Profile `.env` 应进入单独的秘密备份，不进入 Git。

从旧脚本式部署迁移：

1. 停止旧容器。
2. 将旧 `HERMES_DATA_DIR` 内容复制到新的 `data/`，保留现有 `.env`、Memory、Skill、session、pending 和 Kanban 数据。
3. 将旧 `PROJECTS_DIR` 作为新的 `PROJECTS_DIR`，或复制到 `projects/`。
4. 用本部署的 `config.yaml`、SOUL/角色 Skill 逐项人工合并，不覆盖已经批准的运行态 Memory/Skill。
5. 不迁移旧 tooling named volume；使用本包 `tooling/`。
6. 启动新 Compose。旧补丁位于旧容器镜像可写层，不会进入新官方镜像容器。

本仓库不提供自动迁移或验证脚本。
