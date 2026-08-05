# 仓库指南

本文是本仓库所有编码 Agent（包括 Codex、Claude Code 和 Hermes Coder）的唯一项目级指导源。开始工作前应先阅读本文件，并以当前代码、配置和测试为事实依据；若指导与实现发生冲突，应先查明原因并同步修正文档与合同，不得依赖另一份平行规则文件。

## 项目结构与模块组织

`hollysys_controller/` 包含确定性的 Python Controller、RPC 服务、工作流状态机、GitLab/Kanban 适配器和持久化代码。运行策略位于 `controller/config.yaml`；各角色的 Hermes 配置、已审批 Skill 和 Memory 位于 `data/profiles/<profile>/`。容器启动逻辑放在 `container/`，部署和 Schema 工具放在 `scripts/`，消息及工件版式放在 `templates/`，生成的协议契约放在 `schemas/`。测试按 Controller 和运行时关注点组织在 `tests/test_*.py`。不得提交 `cli/`、`skills/`、`controller-data/`、`secrets/` 等生成或本地运行目录，也不得提交项目 worktree。

## 构建、测试与开发命令

本 Python 项目有意不使用 `pyproject.toml`。使用 `requirements-test.txt` 中已锁定的依赖和独立于项目环境的 uv 命令：

```bash
uv run --no-project --with-requirements requirements-test.txt pytest -q
uv run --no-project --with-requirements requirements-test.txt ruff check hollysys_controller tests scripts
uv run --no-project --with-requirements requirements-test.txt python scripts/generate_completion_schema.py
docker compose config --quiet
git diff --check
```

定向验证可使用以下形式：

```bash
uv run --no-project --with-requirements requirements-test.txt pytest -q tests/test_service.py
uv run --no-project --with-requirements requirements-test.txt \
  pytest -q tests/test_compose_contract.py::ComposeContractTests::test_derived_image_pins_base_and_toolchain
```

修改 `external-assets.json` 或 `package-lock.json` 后，宿主机需使用 Node.js 22.20+ 并重新安装生成资产：

```bash
npm ci
npm run assets:install       # 所有已声明的 Skills 和 CLI
npm run assets:skills        # 仅 Skills
npm run assets:cli           # 仅 CLI
```

本地 Compose 启动前需要配置根 `.env`、12 份 `data/profiles/<profile>/.env` 和 Controller token secret，实际配置文件权限保持 `0600`。常用命令如下：

```bash
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=200 controller
docker compose logs --tail=200 hermes
docker compose exec hermes hollysysctl health
docker compose exec controller hollysysctl preflight
docker compose exec controller hollysysctl preflight --deep
```

新部署最初保持 `HOLLYSYS_CONTROLLER_MODE=preflight`。不要仅凭静态测试切换为 `active`；deep preflight 与凭据、项目和 Supervisor 合同绑定，生产验收还必须执行 `README.md` 中的真实 PRD-to-MR 流程。

## 架构与运行时合同

本仓库是围绕固定上游 Hermes Agent 运行时构建的部署与确定性编排层，不是独立 Web 应用。`Dockerfile` 基于 `nousresearch/hermes-agent:v2026.7.30` 构建 Linux/AMD64 派生镜像；源码指纹不匹配时必须失败关闭。

### 运行时拓扑与权限边界

`docker-compose.yaml` 运行两个相互隔离的服务：

- `hermes` 承载 Dashboard、飞书 Gateway、业务 Worker，以及以 `hermes` UID/GID 运行的 Worker Supervisor。
- `controller` 运行不使用 LLM 的 `hollysys_controller` daemon，负责创建正式卡片、推进工作流、绑定 Delivery MR、验证门禁和确定交付终态。Controller 不是 Hermes Profile。
- 两个服务共享 `/opt/data`、`/workspace/projects` 和 `/run/hollysys-controller`，但使用独立 PID namespace。共享目录内包含 Controller RPC Socket `controller.sock` 和 Worker Supervisor Socket `worker-supervisor.sock`；Controller 不得用本容器 PID 检查代替 Supervisor 证据。
- 只有 Controller 挂载 schema v4 SQLite 状态目录和 Maintainer GitLab token secret。Dispatcher 只把人类命令转换成 Controller 调用；Worker 只处理角色范围内的一张卡片。

必须保持 Dispatcher、Controller、Worker、Reviewer 和 GitLab 身份之间的权限划分。Controller 在推进 run 前独立验证 Kanban、GitLab、worktree、branch、MR 和 head 等外部事实。

### Controller 数据流与持久化

`hollysys_controller/daemon.py` 是进程入口；active 模式下并发执行 Kanban 事件轮询、完整对账、带租约的逐 run reconcile worker 和持久化通知 outbox。`hollysys_controller/service.py` 是编排中枢，并协调以下边界：

- `kanban.py` 读取 Hermes board SQLite；受支持的写入必须经过带 CAS/fencing 的 Kanban 接口，不得绕过 attempt 所有权。
- `gitlab.py` 和 `git_auth.py` 验证不可变 PRD/MR 事实、准备共享 checkout 与逐 run worktree，并执行 Controller 授权的 GitLab 操作。Agent 的 Git 访问另由 `container/git/` 下 root-owned wrapper 约束。
- `store.py` 保存 schema v4 的 run、card、attempt、reconcile intent/lease、幂等请求/操作、checked head、依赖故障、merge wait、启动健康状态和通知 outbox。Kanban 与 GitLab 对各自外部事实仍具有权威性。
- `notifier.py` 和 `messages.py` 使用 Dispatcher 的 lark-cli 配置，为不可变飞书来源生成并发送幂等通知。

Controller 是基于持久状态的对账系统。修改外部状态的步骤必须使用稳定 operation/idempotency key；过期工作通过 `state_version`、`expected_run_id` 和 checked-head 比较被拒绝。不得用进程内 continuation、聊天或 session 状态代替工作流状态。

### Worker 回收与 attempt fencing

`WorkerRecoveryCoordinator` 是 Controller watchdog、人类 abort、异常清理和重派的统一入口。生产环境只通过 Unix Socket adapter 请求 Hermes PID namespace 内的 Supervisor；测试可使用内存 fake。

- Socket 缺失、超时、身份不符、PID 重用或证据不完整时，只能记录 `liveness_unconfirmed`，不得 reclaim、archive、修改 MR 或重派。
- heartbeat 新鲜时，无论 progress 是否超时都不得 probe、terminate 或 reclaim。只有 heartbeat 与 progress 同时超时、Supervisor 确认退出或成功终止，并且完整 CAS 复核仍匹配时，才允许 `reclaim --expected-run-id --expected-worker-pid`。
- 所有父 Worker Kanban mutation 必须携带 `expected_run_id`；旧 attempt 的 heartbeat、progress、comment、attachment、create/link、complete、block 和 failure 都应安全 no-op。
- Tool Executor 在每次工具调用前核对 `current_run_id`；失去所有权的会话以 `stale_attempt` 结束，不得再执行 terminal、patch 或 Git 操作。
- `worker_exited` 只能来自真实 `waitpid`/reap 或 Supervisor 确认终止。禁止把 Agent 自报完成或 terminal Kanban 工具成功伪装成进程退出。
- 人类 abort 先进入 `aborting`；只有 Supervisor 确认进程树退出后才能 reclaim/archive 并标记 aborted。终止失败时保持 `aborting`。

健康与状态查询只读取持久化的 Supervisor 快照，不在查询路径实时调用 Socket。历史 `reclaimed` attempt 不得进入 `stale_workers`，同一 card/worktree 出现重叠有效 attempt 必须显式报告。

### 工作流与严格合同

`hollysys_controller/workflow.py` 定义纯路由状态机：

```text
spec-write -> spec-review -> plan-write -> plan-review
-> tasks-write -> tasks-review -> implement -> test -> code-review
```

文档审查失败时返回对应 producer；达到审查次数上限后进入一次性 finalization。测试或代码审查失败可在额度内返回 implement。测试和审查证据必须绑定同一个 MR head，任何新 push 都使旧代码门禁失效。最终审查成功后进入 Ready/交付终态；Controller 不自动合并 MR。

`hollysys_controller/models.py` 是严格协议的权威定义。Pydantic 模型禁止额外字段并强制 stage/outcome/gate 跨字段不变量。`scripts/generate_completion_schema.py` 根据 `CompletionMetadata` 生成 `schemas/card-completion.schema.json`；修改 completion 语义时同步更新模型、生成器/schema、路由、service 校验和合同测试。

`controller/config.yaml` 是不含秘密的策略层；每个阶段必须恰好一个 assignee 和非空 Skill 列表。新增或重命名阶段/角色时，同步更新 enum/路由、Controller 配置、`data/profiles/<profile>/` 合同、completion schema/测试、消息与模板预期。

### 生成资产与容器补丁

`external-assets.json` 与 `scripts/install-external-assets.mjs` 是第三方 Skills 和 Linux/AMD64 CLI 的唯一声明、锁定与安装机制。根目录 `skills/`、`cli/` 和 `node_modules/` 是生成资产，不得提交；`data/profiles/*/skills/hollysys-*` 是受 Git 跟踪的 fleet 角色合同。

`container/` 负责同步 Profile Skills 和 lark-cli 配置、安装受约束 Git wrapper、应用带源文件指纹校验的 Hermes 补丁，并准备离线缓存。该目录的修改属于运行时合同变更，必须由 Compose、Profile 同步、补丁、仓库边界及长运行恢复测试覆盖。

## 编码风格与命名约定

Python 使用四空格缩进、类型注解、`from __future__ import annotations` 和 Ruff。函数及模块使用 `snake_case`，类使用 `PascalCase`，枚举和协议名称应清晰描述其含义。Pydantic 模型保持失败关闭（`extra="forbid"`），Controller 行为必须确定且幂等。Shell 脚本使用 Bash 严格模式，导出的配置使用大写名称。除非同时更新生成器，否则不要手工修改生成的 Schema。

## 测试规范

测试遵循 `unittest.TestCase` 约定，但通过 pytest 运行。文件命名为 `test_<area>.py`，类命名为 `<Area>Tests`，方法命名为 `test_<behavior>`。状态转换、持久化与恢复、契约校验以及 Compose/Profile 不变量的变更必须补充回归测试。

Worker Supervisor、跨容器 PID namespace 或进程树终止发生变化时，在派生镜像构建后额外执行：

```bash
scripts/test-worker-supervisor-containers.sh hollysys-hermes-agents:latest
```

该脚本只能使用临时容器和临时卷，不得加入或重建现有 Compose 项目。仓库未规定数值化覆盖率门槛；提交前至少应通过完整 pytest、Schema 重新生成、Ruff、Compose 解析和 `git diff --check`。静态检查或容器定向测试通过不等于生产 E2E 已验证。

## 提交与合并请求规范

提交历史遵循 Conventional Commits，常见格式包括 `feat(controller): ...`、`fix(runtime): ...` 和 `chore(profiles): ...`；破坏性变更使用 `!`。提交范围应聚焦，不要夹带无关的 `tmp/` 或 `reports/` 文件。合并请求应说明目的与运维风险，关联 Issue/PRD，列出实际执行的验证命令和结果，并注明对配置、Schema、迁移或部署的影响。只有 Dashboard 或消息格式发生变化时才需要附截图。

Coder 按冻结 TASKS 的 execution wave 实施时，每波只能提交路径不重叠且已定向验证的改动；子 Agent 不得 commit/push 或修改父卡片。中间 checkpoint 保持本地，只有全部 wave 和完整门禁通过后才统一 push。提交前检查 branch/upstream/remotes 和 staged diff，精确暂存目标路径，并比较本地与远端完整 SHA；除非用户明确要求，不得纳入 `tmp/`、`reports/` 或并行产生的文件。

## 部署与验收约束

Protocol v4 只支持全新状态，不迁移或续跑旧 Controller DB、Kanban、session、日志、cache、pending、worktree 或旧 MR binding。新安装必须以 `preflight` 启动，并按以下原则操作：

- 静态 preflight 后执行用户已授权测试项目上的 deep preflight，验证 Profile 身份、Git HTTPS 传输、root-owned wrapper、token-free origin、Writer dry-run、Reviewer 拒绝、lark-cli 配置和 Controller 可写目录。
- Hermes 与 Supervisor Socket 就绪后，再执行 `hollysysctl preflight --deep --require-supervisor`；active 启动只接受绑定当前凭据和 Supervisor 协议证明的摘要。
- token、准入项目、Profile 合同或 root-owned wrapper 变化后，先切回 `preflight`，重新执行 deep preflight，再恢复 `active`。
- 不得在存在活动 Worker、会话或 run 时热更新。远端部署、容器重建、数据库备份或模式切换必须获得明确授权；本地实现请求不自动授权远端操作。
- 不清理现有目录、卷、镜像、run 数据或其他 Compose 项目。部署前备份实际 SQLite、`.env`、源文件和旧镜像 ID，并保持可直接恢复的无 schema 迁移回滚路径。
- `hollysysctl preflight` 必须连接 Controller RPC，由已降权 daemon 执行；不要在 Controller 容器中另建本地 `ControllerService`，以免 Profile HOME 出现 root-owned 文件并导致凭据合同漂移。
- 容器 healthcheck 使用 liveness，只验证本地 Controller/store/RPC；GitLab 或 Kanban 短时故障不应触发整容器重启。运维和 Dispatcher 使用 readiness 查看最近对账、外部依赖、outbox、stale/overlap attempt 与 Profile 准入状态。
- `start` 的 GitLab/workspace/offline-cache 初始化超过同步窗口时，应返回同一幂等请求的 `request_status=running` 快照，不能因客户端超时重复创建任务。
- 当前版本只支持单机、单 Controller 和固定完整流程。`scripts/deploy-remote.sh` 只负责打包新的远程部署，不执行 deep preflight、不切换 active，也不构成生产验收。

生产验收必须覆盖真实 PRD→SPEC→PLAN→TASKS→implement→test→按条件 code-review→MR Ready/终态，以及长 heartbeat 不重派、受控崩溃仅重派一次、无重叠 worktree 写入、旧 attempt 无法改写 MR/head/provenance、abort 两阶段语义、通知幂等和 Controller/Supervisor 重启恢复。静态结果、健康容器或空 outbox 都不是这类 E2E 的替代证据。

## 安全与 Agent 边界

禁止提交 `.env`、Token、认证状态、Controller 数据库、日志、run scratch 原始证据或项目 worktree。输出、progress、health、通知和 RPC 响应不得包含命令输出、进程环境或凭据。完整超限工具输出只能写入权限 `0600` 的既有 run scratch，并返回 path、bytes 和 SHA-256。

必须保持 Dispatcher/Controller 权限边界、Reviewer 禁止推送、每个 run 的 source key/generation/checkout/worktree/branch/MR/head 溯源，以及 `memory.write_approval` / `skills.write_approval` 设置。delegated child 只能返回研究结果，不得修改父卡片；父 Coder 才能发送受节流的结构化 progress。任何修改这些合同、放宽文件系统/Git 权限、绕过 Supervisor 或降低 attempt fencing 的变更都必须经过明确审查并补充失败关闭测试。
