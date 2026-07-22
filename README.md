# Hermes Kanban SDD Agent Fleet

本仓库提供一套可部署的 Hermes Agent profiles：人类在飞书中只启动一次，之后由 Hermes Kanban 持久调度 `PRD → SPEC → PLAN → TASKS → code → test → review → merge`。正常路径无需人工逐段唤醒；只有需求冲突、能力/凭据故障或返工预算耗尽才暂停。

## 部署结论

采用 Hermes 官方推荐的**一个容器、多 profiles**：

- `dispatcher` 是唯一 Feishu gateway、Kanban 编排者和合并身份。
- 其他 Agent 不启动 gateway；卡片 ready 后由 Kanban 以独立 profile 进程拉起。
- 全部 profiles 和每项目 Kanban SQLite board 位于同一持久卷；不要让第二个 active 容器共享该卷。
- 每项目一个 board；每个 PRD run 用 `tenant=run_key` 分组。
- 同一 run 内 SPEC 严格串行，每个 SPEC 一个代码 MR。

只有需要独立网络、资源或合规边界时才拆容器；拆分后必须使用独立数据卷和外部 worker-lane 适配，不能共享 SQLite。

## 内容

```text
profiles/<agent>/
├── .gitignore              # 排除凭据、memory、session 和运行状态
├── distribution.yaml       # Hermes profile distribution manifest
├── profile.yaml            # Kanban 路由描述
├── SOUL.md                  # 角色与边界
├── config.yaml              # toolsets、memory、terminal、Kanban
├── .env.template            # 安装后生成 .env.EXAMPLE
├── bootstrap/memories/      # MEMORY.md / USER.md 首装种子
└── skills/<role>/SKILL.md   # 角色操作合同
```

包含 `dispatcher`、`prd-writer`、`fde`、SPEC/PLAN/TASKS 的 producer-reviewer 对、`coder`、`tester`、`code-reviewer`。Docker 构建时按 [版本锁](config/skills-lock.yaml) 加入 GitLab 官方 `glab` Skill、Lark 官方 `lark-shared`/`lark-im` Skill，并安装对应 CLI。

Hermes distribution 会硬性排除真实 `memories/`。因此本仓库只提供无用户数据的种子，容器首装时仅在目标文件不存在时写入；升级不会覆盖 Agent 已形成的记忆。

## 首次部署

前置条件：Docker Compose、一个已发布的 Feishu/Lark bot App、已挂载的 GitLab 项目 checkout，以及各 Agent 的最小权限 token。

```bash
cp .env.example .env
# 编辑 .env：模型、Feishu、各 Agent GitLab token，以及 producer 的 commit name/email

mkdir -p .runtime/projects
git clone <gitlab-project-url> .runtime/projects/<project>

./scripts/verify-bundle.sh
docker compose build
docker compose up -d
```

启动脚本会自动：

1. 在 `/opt/data/profiles/` 安装 12 个 profiles；
2. 只在缺失时创建 `memories/MEMORY.md` 与 `USER.md`；
3. 为每个 profile 生成隔离的 `.env`，只给 dispatcher Feishu 密钥；
4. 清空 s6 的 fleet-wide 启动环境，防止其他 Agent 继承别人的 token；
5. 配置 `FLEET_MODEL`，并用同一个 App ID/Secret 初始化 dispatcher 的 `lark-cli` bot 身份；
6. 启动且只启动 dispatcher gateway。

检查：

```bash
docker compose exec hermes hermes version
docker compose exec hermes hermes profile list
docker compose exec hermes hermes -p dispatcher gateway status
docker compose exec hermes hermes -p dispatcher kanban boards list
docker compose logs --tail=100 hermes
```

## GitLab 与 board 初始化

默认分支必须设为 protected：Agent 不得直接 push；producer 只能写功能分支/MR；reviewer/tester 只读代码并写评论；只有 dispatcher 可以 merge。每个 profile 的 token 是不同 GitLab 身份，便于归因和撤销。

为项目建立 board，路径必须是容器内绝对 checkout 路径：

```bash
./scripts/create-board.sh \
  sdd-cxtc \
  mes/cxtc \
  /workspace/projects/cxtc
```

然后分别验证身份与仓库 remote；任何失败都应作为 `capability` block 处理，不能临时把全部 Agent 提升为 Maintainer。

## 使用

在 Feishu 允许名单内向 dispatcher 发送：

```text
实现 PRD <已合入的 GitLab PRD MR URL>
状态 <run_key>
暂停 <run_key>
继续 <run_key>
取消 <run_key>
```

Dispatcher 会验证 PRD 已合入目标分支并创建幂等 `RUN-INIT` 卡。后续 Agent 只从 Kanban parent handoff 与 GitLab live state 恢复，不依赖聊天上下文。

观察与运维：

```bash
docker compose exec hermes hermes -p dispatcher kanban --board <board> watch
docker compose exec hermes hermes -p dispatcher kanban --board <board> list
docker compose exec hermes hermes -p dispatcher kanban --board <board> runs <task-id>
docker compose exec hermes hermes -p dispatcher kanban --board <board> tail <task-id>
```

设计返工最多 3 轮，代码返工最多 5 轮。tester 与 code-reviewer 的 pass 必须绑定同一当前 head；dispatcher 最终用 GitLab merge API 的 `sha=<checked_head>` 合并。

## 升级与恢复

```bash
docker compose build --pull
docker compose up -d
```

默认保留已部署 `config.yaml`。接受本仓库的新托管配置时，将 `.env` 中 `FLEET_FORCE_CONFIG=1`，重建一次后恢复为 `0`。`SOUL.md`、`profile.yaml` 和本地 Skills 随镜像同步；`.env`、memory、sessions、Kanban DB 和日志位于持久卷。

备份时停止 active 容器，再备份整个 `HERMES_DATA_DIR`；恢复后先核对 profiles、官方 Skill revision、项目路径、token 身份和 board 指针，再启动 gateway。不得同时运行两个容器写同一数据目录。

## 生产边界

- `kanban.auto_decompose=false`；只有类型化 dispatcher Skill 扩展 SDD DAG。
- Hermes 0.18.2 的专业 worker 仍能看到 `kanban_create/link`。生产前必须在 tool registration/handler 层仅允许 dispatcher worker 调用，并拒绝非 dispatcher 创建的 ready 卡；SOUL/Skill 不能替代权限控制。
- Hermes 原生 gateway 是唯一 `im.message.receive_v1` consumer；不得再运行 `lark-cli event consume`。
- 上线前必须验证：各 Agent 独立身份和最小权限、protected branch、单 SPEC 完整 E2E、重复消息/响应丢失幂等、worker/gateway 崩溃恢复、head 漂移阻断、冷备恢复。未全部通过时不得称为生产自治 E2E。

研究基线：[Hermes distributions](https://hermes-agent.nousresearch.com/docs/user-guide/profile-distributions)、[Docker](https://hermes-agent.nousresearch.com/docs/user-guide/docker)、[Kanban](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban)、[worker lanes](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban-worker-lanes)、[Feishu](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/feishu)、[GitLab glab Skill](https://gitlab.com/gitlab-org/ai/skills/-/tree/main/skills/glab)、[Lark CLI Skills](https://github.com/larksuite/cli/tree/main/skills)。实际 revision 见 [版本锁](config/skills-lock.yaml)。
