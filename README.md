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
nousresearch/hermes-agent:v2026.7.20@sha256:a6ce64e2038867885c2c90f6602425e6e70293d5e6d952a0e603a99265e01c40
```

Compose 配置了 `pull_policy: never`，不会构建镜像，也不会静默拉取其他 tag。profiles、模板、schema、脚本、锁文件和 Hermes 0.19.0 小型运行时补丁均以只读 volume 挂载。glab、lark-cli 和外部 Skills 位于独立 named volume，更新它们不需要重建 Hermes 镜像。

主要持久目录：

| 宿主机变量 | 容器路径 | 内容 |
| --- | --- | --- |
| `HERMES_DATA_DIR` | `/opt/data` | profiles、独立 `.env`、Kanban SQLite、sessions、memory、Agent Skills、pending 审批队列、Gateway 和 lark-cli 状态 |
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

#### 1.3.1 Hermes 内置 Skill 边界

这组 Agent 面向软件开发交付。所有 Profile 通过 `config.yaml` 的全局 `skills.disabled` 禁用与本系统无关的内置 Skill，而不是删除官方镜像中的文件：

| 类别 | 禁用的 Skill name |
| --- | --- |
| 视频、影音和 3D 制作 | `ascii-video`、`comfyui`、`manim-video`、`touchdesigner-mcp`、`hyperframes`、`kanban-video-orchestrator`、`blender-mcp`、`gif-search`、`youtube-content` |
| 音乐和音频生成 | `songwriting-and-ai-music`、`heartmula`、`songsee`、`audiocraft-audio-generation` |
| 家庭物联网 | `openhue` |
| 游戏 | `minecraft-modpack-server`、`pokemon-player` |

其中部分是当前镜像自带 Skill，部分是官方 optional Skill；提前写入 optional Skill 名称可确保以后误安装时仍保持禁用。禁用规则按 Skill frontmatter 中的精确 `name` 匹配，不支持分类通配符。

以下内容不受影响：

- 每个 Profile 自己的一个角色 Skill。
- `/opt/fleet/vendor/current/gitlab/skills` 和 Gateway 使用的 Lark 外部 Skills。
- 软件开发有用的架构图、Excalidraw、Web 设计、GitHub、MLOps、数据分析和软件工程 Skills。

新增或移除禁用项必须由人类审查后同步修改全部 Profile 的 `skills.disabled`，并在 `scripts/verify-bundle.sh` 的统一禁用集断言中更新。不要直接修改 `/opt/hermes/skills`。所有 Profile 还固定 `curator.prune_builtins: false`；运行时补丁根据 `.bundled_manifest` 在 `skill_manage` handler 层拒绝对 Hermes bundled Skill 的 `edit`、`patch`、`delete`、`write_file` 和 `remove_file`。manifest 缺失或不可读时同样 fail closed。

#### 1.3.2 Agent Skill 的永久数据边界

Compose 的 `./profiles:/opt/fleet/profiles:ro` 只提供部署种子，不是运行态 Skill 写入位置。每个 Agent 的本地 Skill 事实源是：

```text
${HERMES_DATA_DIR}/profiles/<profile>/skills
```

该目录属于 `/opt/data` 持久边界并对对应 Agent 可写。`skills.write_approval: true` 产生的待审批修改位于同一 Profile 的 `pending/skills/`，bootstrap 不读取、不清空也不覆盖它。Hermes 的 Profile 本地 Skills 和 pending 审批模型参见[官方 Skills 文档](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/)。

初始化规则：

- 新 Profile 的本地 `skills/` 为空时，bootstrap 原子复制仓库随附的唯一 `sdd-*` 角色 Skill，然后写入 `/opt/data/.fleet/skills-v1/<profile>` 标记。
- 已有 Profile 首次迁移时直接采用当前本地目录；不会比较或复制仓库内容。缺少唯一的预期角色 Skill 时停止启动，必须由人类从备份恢复。
- 标记存在后，fleet bootstrap 每次启动只验证被记录的角色 Skill 仍存在，永不新增、删除或覆盖本地 Skill。仓库中同名角色 Skill 的更新或删除不会改变运行环境。
- 人类批准后，角色 Skill 的正文和支持文件、Agent 新建 Skill 及其支持文件永久保留；`sdd-*` 角色 Skill 允许审批后编辑，但运行时拒绝删除。其他 Agent 自建 Skill 仍可按审批流程创建、编辑和删除。
- GitLab/Lark external Skills 继续来自只读挂载；Hermes bundled Skills 继续由官方 provenance 管理，但 Agent 无权修改。

因此，“永久”表示随 `HERMES_DATA_DIR` 跨容器重启、镜像升级和仓库升级保存并进入备份，不表示自动回写 GitLab。需要采用仓库中的新版角色 Skill 时，必须由人类审查差异后，通过现有 Skill 审批流程明确修改运行态副本。

查看某个 Profile 实际启用的 Skill：

```bash
docker compose exec hermes hermes -p dispatcher skills list --enabled-only
docker compose exec hermes hermes -p coder skills list --enabled-only
```

Dashboard 的 Profile 切换器也可以查看和临时切换 Skill；变更在新会话或 Gateway 重启后生效。仓库 `config.yaml` 仍是托管配置种子，接受新版配置时按升级流程使用一次 `FLEET_FORCE_CONFIG=1`，确认后恢复为 `0`；该开关不会同步或覆盖持久化 `skills/`。

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

所有 Agent 的 GitLab 项目、仓库元数据、MR、pipeline、discussion、comment、Issue 和 merge 操作只使用 bundle 锁定的 `glab` CLI 或已安装官方 `glab` Skill。不得临时改用 raw HTTP/`curl`、自建 SDK、浏览器或手工 UI。各角色 Skill 明确要求的本地 `git` checkout、检查、commit 和 push 不受此限制。

正式 run 的交付分支只在卡片指定的 Hermes 共享 worktree 中存在可编辑工作副本；该分支由 Agent 按串行阶段维护，人类不参与修改。dispatcher 完成初次准备或恢复后，各阶段以该 worktree 为本地代码事实源，不执行例行 `git fetch origin` 或来回 pull。只有缺少 ref/worktree、已有证据表明本地/远端 head 不一致、push 被拒绝，或 continuation 明确要求 `live_reconcile_required` 时才 fetch，并记录原因；MR、pipeline 和 discussion 的当前状态仍通过 `glab` 查询。

PRD 未说明或表述模糊本身不是请求人类、`needs_input` 或 `scope_gap` 的理由。Agent 依次依据明确验收与约束、仓库现状与惯例、已批准的上游工件、兼容性/安全性和最小可逆范围自主决策。只有证据互相冲突且不存在保持验收的安全选择，或确实缺少项目身份、权限、凭据、环境或能力时才暂停并给出具体的人类动作。

影响用户可见范围或验收、公共接口、数据模型/迁移、安全/权限、兼容性、恢复/回滚或必需测试门禁的选择属于**关键自主决策**。交付 MR 尚不存在时，前置角色把决策明确交给 `spec-writer`；`spec-writer` 首次创建 MR 前，将全部前置决策和本阶段决策直接写入 MR description 的“关键自主决策”表，没有时写“无”。MR 已存在后，每张卡使用 `templates/decision-comment.md` 在同一 MR 发布或更新一条带稳定 marker、包含本卡全部关键决策的幂等评论；reviewer/tester 可以把同样字段合并进已有 gate comment。存在关键决策时，正式卡 completion metadata 的 `gitlab_urls` 必须包含对应 MR description 或 comment URL；没有关键决策时不制造空评论。

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

#### 2.4.1 Kanban 双卡协议

每张 dispatcher gate 卡都必须先创建一个逻辑双卡对，再完成自己：

```text
dispatcher gate G (running)
  ├─ worker W，parent=G，key=<run>:<stage>:<iteration>:work
  └─ dispatcher continuation C，parent=W，key=<run>:<stage>:<iteration>:continue

G complete → W ready → W complete → C ready
```

创建顺序固定为“创建或复用 W → 创建或复用 C → 核对两条依赖 → 完成 G”。如果任一步失败，G 不得完成；重试必须使用同一组稳定 idempotency key。这样容器重启、worker crash 或重复消息只会恢复原卡，不会产生重复工作卡或续跑卡。

C 只保存父卡 ID、父阶段和 `live_reconcile_required=true`。它被唤醒后先读取 W 的 completion metadata 并按 schema 校验，再重新查询 GitLab 的分支、MR、评论、pipeline、discussion 和当前 head。创建 C 时不得预填未知的 `head_sha`、`artifact_digest`、review/pipeline/merge 结论。

正式 SDD 卡的 completion metadata 是一个扁平 v2 对象；卡片正文中的 `identity`、`workspace`、`source`、`delivery` 分段不能代替顶层必填字段。Hermes 公共完成入口会在写入前校验 schema 和真实 `kanban_card_id`，因此 Tool、CLI、Dashboard 都不能把残缺 handoff 标记为完成；校验失败时原卡保持可重试。

历史卡片若已保存残缺 metadata，dispatcher 不得根据评论或卡片正文推断放行。只允许使用 `hermes kanban edit` 审计回填完整对象，或创建新的 review/work pair 重跑。

SPEC/PLAN/TASKS 的 write、review、rework，以及 implement、test、code-review、code-rework 均执行同一协议。merge 也作为受检 work card，后接 `run-complete` continuation；只有 checked-head merge 完成后，`run-complete` 才能进入 ready。

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
- 只有证据互相冲突且不存在保持验收的安全选择、能力/凭据/环境故障或预算耗尽时才进入暂停，并在飞书说明原因、证据、已尝试动作和需要的人类决定；普通 PRD 模糊项和可逆的合同内决策继续自主处理并按上述规则记录。

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

合并成功后，dispatcher 使用 merge commit SHA 在 MR 留下 PRD、SPEC、PLAN、TASKS 的排序后永久 blob 链接，清理成功 run 的 worktree，并在原飞书群聊或话题回复。回复中必须 `@` 原发起人。

## 3. 部署说明

### 3.1 目标环境与资源

目标环境：

- Ubuntu Linux，AMD64/x86_64。
- Docker Engine 已安装并运行。
- Docker Compose v2，可使用 `docker compose`。
- 宿主机已经存在锁定的 AMD64 manifest：`nousresearch/hermes-agent:v2026.7.20@sha256:a6ce64e2038867885c2c90f6602425e6e70293d5e6d952a0e603a99265e01c40`。
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
docker image inspect 'nousresearch/hermes-agent:v2026.7.20@sha256:a6ce64e2038867885c2c90f6602425e6e70293d5e6d952a0e603a99265e01c40' >/dev/null
```

期望 `uname -s` 为 `Linux`，`uname -m` 为 `x86_64`。

### 3.2 获取部署包

将本仓库 clone 到 Ubuntu 的固定目录，然后进入仓库根目录。不要只复制单个 Compose 文件；启动依赖本仓库中的 profiles、模板、schema、补丁、锁文件和脚本。

```bash
git clone <本部署包仓库URL> hermes-agents-by-kanban
cd hermes-agents-by-kanban
git checkout --detach <本次批准的40位commit或release tag>
git status --short
```

全新 clone 的 `git status --short` 应为空。部署不得从浮动 `main` 直接启动；根 `.env` 中的 `FLEET_BUNDLE_REF` 会锁定当前完整 commit，运行态验收要求它与 `HEAD` 一致。

### 3.3 配置根 `.env`

先确认锁定镜像已在本机，然后从真实终端运行：

```bash
./scripts/init-deployment-env.sh
```

脚本要求仓库干净且 `git fsck` 通过，自动写入当前完整 commit、`PUID`/`PGID`、默认 Dashboard 用户名 `admin`，并交互式读取用户指定的初始密码。密码只经标准输入传给锁定镜像中的 Hermes `hash_password()`；根 `.env` 仅保存单引号包裹的 scrypt hash 和 `openssl rand -base64 32` 生成的独立 signing secret。脚本不会把明文密码写入文件、日志或命令行参数，并把根 `.env` 固定为普通文件、权限 `0600`。

使用该 UID 对应的宿主用户执行后续初始化，不要用 `sudo` 创建 profile 凭据文件。

至少配置：

```dotenv
HERMES_IMAGE=nousresearch/hermes-agent:v2026.7.20@sha256:a6ce64e2038867885c2c90f6602425e6e70293d5e6d952a0e603a99265e01c40
FLEET_BUNDLE_REF=<当前40位commit，由初始化脚本写入>
HERMES_DATA_DIR=./.runtime/hermes
PROJECTS_DIR=./.runtime/projects
PUID=<id -u 输出>
PGID=<id -g 输出>

HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin
HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH='<初始化脚本生成的scrypt hash>'
HERMES_DASHBOARD_BASIC_AUTH_SECRET='<独立32字节随机signing secret的base64>'

FLEET_MODEL=<Hermes 支持的 provider/model>
FLEET_MODEL_BASE_URL=<模型服务端点；OpenAI Codex 使用 https://chatgpt.com/backend-api/codex>
<与所选模型对应的 API_KEY>=<模型服务密钥>

GIT_COMMIT_NAME_PRD_WRITER="Hermes PRD Writer"
GIT_COMMIT_EMAIL_PRD_WRITER=prd-writer-bot@hermes.invalid
GIT_COMMIT_NAME_SPEC_WRITER="Hermes SPEC Writer"
GIT_COMMIT_EMAIL_SPEC_WRITER=spec-writer-bot@hermes.invalid
GIT_COMMIT_NAME_PLANNER="Hermes Planner"
GIT_COMMIT_EMAIL_PLANNER=planner-bot@hermes.invalid
GIT_COMMIT_NAME_TASKER="Hermes Tasker"
GIT_COMMIT_EMAIL_TASKER=tasker-bot@hermes.invalid
GIT_COMMIT_NAME_CODER="Hermes Coder"
GIT_COMMIT_EMAIL_CODER=coder-bot@hermes.invalid
```

`.invalid` 是保留的不可投递域名，适合标识这些提交来自自动化 Agent，不需要注册五个真实邮箱。如果私有 GitLab 启用了“提交者邮箱必须已验证”之类的 push rule，则应由管理员为五个 bot 身份创建并验证真实的 no-reply alias，再替换上述默认值。

只填写所用模型对应的 Key。GitLab、飞书秘密不得写入根 `.env`；根 `.env` 也不得出现 `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` 明文变量，signing secret 不得复用登录密码。

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
7. 在飞书开发者后台关闭机器人私聊能力，并将机器人加入需要使用的群聊。Hermes 不额外维护成员 allowlist；群内任何成员必须显式 `@` 机器人才能触发。

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
FEISHU_ALLOW_ALL_USERS=true
FEISHU_ALLOWED_USERS=
FEISHU_HOME_CHANNEL=
FEISHU_GROUP_POLICY=open
FEISHU_REQUIRE_MENTION=true
FEISHU_ALLOW_BOTS=mentions
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
git diff --check
git fsck --full
./scripts/verify-bundle.sh
docker compose up -d
```

首次启动顺序：

1. `tooling-sync` 下载并验证锁定工具，写入 named volume 后退出 0。
2. `hermes` 启动，确认版本精确为 0.19.0 并幂等应用 Kanban guard patch。
3. bootstrap 安装 12 个 profiles；为新 Profile 初始化一次角色 Skill，为已有 Profile 建立不覆盖的迁移标记，并验证角色 Skill 仍存在。
4. 为五个 producer 配置独立 Git commit identity。
5. 验证 12 个 GitLab 身份和 3 个飞书身份，将三个飞书 `.env` 绑定到各自 lark-cli 加密存储。
6. s6 启动 dispatcher、prd-writer、fde 三个 Gateway，以及受 basic auth 保护的 Dashboard。

若 `tooling-sync` 失败，Hermes 不会启动。先修复网络或锁文件问题，不要绕过 checksum。

如果 `HERMES_DATA_DIR` 已有旧 profile 配置，首次启动会直接采用各 Profile 当前的 `skills/` 并写初始化标记，不复制仓库 Skill。必须先确认每个 Profile 仍有唯一的预期 `sdd-*` 角色 Skill；缺失时 bootstrap 会停止，不能用重新复制种子的方式静默恢复。托管 `config.yaml` 升级只允许将根 `.env` 中的 `FLEET_FORCE_CONFIG` 临时改为 `1` 启动一次。确认 12 份持久化配置已包含 `memory.write_approval: true`、`skills.write_approval: true`、`curator.prune_builtins: false` 和本次 Kanban 配置后，立即改回 `0` 并重启 `hermes`。`verify-runtime.sh` 会拒绝 `FLEET_FORCE_CONFIG=1`，避免它长期覆盖运行数据；该开关不会覆盖 Skills。

### 3.8 一键运行态验收

容器启动后执行：

```bash
./scripts/verify-runtime.sh
```

该命令只读取 Git、根 `.env`、Docker、Hermes、GitLab 和本地加密配置状态，不创建 Issue、分支、MR，也不发送飞书消息。成功时应报告：

- checkout 干净、`git fsck` 正常，`HEAD` 等于固定 `FLEET_BUNDLE_REF`。
- Linux AMD64、运行容器确实使用锁定 manifest digest，Compose service 正常。
- Hermes 0.19.0、Kanban guard、内置 Skill 修改保护、锁定 tooling 均正确。
- 12 个 profiles 存在，本地 Skill 目录可写、初始化标记和角色 Skill 存在，`home_mode: profile`、Memory/Skill 写审批和 `curator.prune_builtins=false` 生效；三个 Gateway running，九个 worker 未启动 Gateway。
- 12 个 GitLab API 身份不同且角色精确匹配；所有 profile 的 GitLab host/allowlist 一致。
- 三个 lark-cli 状态目录独立且权限正确。
- Dashboard s6 service running，`/api/status` 返回 `auth_required=true` 且 `auth_providers` 包含 `basic`；宿主端口只绑定 `127.0.0.1:9119`。

失败时按输出中的 profile 和检查项处理；脚本不会打印 token 或 App Secret。脚本通过后仍需从宿主机浏览器访问 `http://127.0.0.1:9119`，用默认用户名和用户指定的初始密码完成一次真实登录；不得把密码放入 `curl` 参数、shell history 或验收日志。

### 3.9 首次消息 smoke test

运行态验收通过后，分别验证三个机器人原渠道回复：

1. 在群聊中 `@prd-writer` 发送一条无敏感信息的 PRD 澄清请求，确认群聊/话题回复正确。
2. 向 `fde` 发送一条测试现场反馈，明确要求只分析、不创建 Issue，确认分类与回复渠道。
3. 向 `dispatcher` 发送缺少 URL 的 `实现 PRD`，确认它继续询问且没有创建 run。
4. 最后使用专用测试项目和已合入测试 PRD 执行完整启动协议。

前三项验证 Gateway 和原渠道回复，不证明 GitLab 写权限或完整自治。完整测试项目应继续验证 branch/MR/pipeline/discussion、checked-head merge、重启恢复和故障注入。

### 3.10 内网受控试运行放行验收

在专用测试项目保存每一步的命令、card/MR URL、完整 SHA、时间和结果。以下七组必须全部通过：

1. 静态：checkout 干净，`git diff --check`、`git fsck --full`、`verify-bundle.sh`、全部测试和 Compose 解析通过。
2. Ubuntu 运行：`verify-runtime.sh` 全部通过，包括固定 commit、镜像 digest、Hermes/补丁/tooling、12 profiles、3 Gateways 和 Dashboard basic auth。
3. Kanban：观察真实 `dispatcher gate → worker → dispatcher continuation` 状态链；非 dispatcher 创建/链接被 handler 拒绝；`created_by != dispatcher` 的 ready 卡不被调度；重复消息和重复 reconcile 不增加卡片数量。
4. 身份：12 个 GitLab 用户互异且角色精确为表中级别；五个 producer 只能向测试分支各执行一次可回收 push；reviewer/tester/FDE 的 push 必须被拒绝。
5. 飞书：三个机器人均通过群聊显式 `@` 触发并在原群聊/话题回复；向 dispatcher 发送缺少正式启动参数的消息时不得创建 run。
6. 完整 E2E：一个已合入测试 PRD 串行通过 SPEC、PLAN、TASKS、实现、测试、代码审查和 `sha=<checked_head>` merge；MR 留下 merge commit 永久链接，原飞书会话收到一次完成通知。
7. 故障注入：分别验证容器重启、worker crash、token 失效、pipeline 失败、阻塞 discussion、head drift 和重复消息；恢复后不得出现重复分支、MR、card 或通知，旧 head 的 test/review 结论不得复用。

全部证据通过后，放行结论才是 **Go：内网受控试运行**。首发接受单容器同 UID 下 profile 凭据不是强安全边界，测试项目只使用最小权限、可随时撤销的 token；本阶段不增加多容器、独立 Unix 用户或外部凭据代理。任一真实 GitLab、飞书、checked-head merge 或恢复测试未完成时，结论仍为 **No-Go**，且不得声明生产自治部署完成。

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

### 4.4 更新 glab、lark-cli 或 external Skills

先更新 `config/skills-lock.yaml` 中的精确版本/revision，再执行：

```bash
docker compose stop hermes
docker compose run --rm tooling-sync
docker compose up -d hermes
./scripts/verify-runtime.sh
```

tooling 使用不可变 release 目录并原子切换 `current` 链接。不要在 Hermes 正在运行时切换 tooling 版本。

该流程只更新 named tooling volume 中的 GitLab/Lark external Skills。修改仓库 `profiles/<profile>/skills/` 不会自动进入已经初始化的 Profile；若人类决定采用新版角色 Skill，先审查差异，再让对应 Agent 通过 `skills.write_approval: true` 的审批流程修改持久化副本及支持文件。

### 4.5 升级 Hermes

1. 先把目标官方镜像放入宿主机。
2. 同步修改根 `.env`、Compose 默认 tag、`skills-lock.yaml` 中的 Hermes release/revision、profiles 最低版本和运行时补丁。
3. 在隔离环境验证新版本补丁仍可应用。
4. 停止生产容器、备份数据后升级。

运行时补丁会拒绝非 0.19.0 版本，因此不能只修改镜像 tag。默认不会覆盖持久数据中的 `config.yaml`；确需接受仓库托管配置时，将 `FLEET_FORCE_CONFIG=1` 启动一次，确认后立即恢复为 `0`。镜像或仓库升级均不会覆盖 Profile 本地 Skills，升级前后必须保留整个 `HERMES_DATA_DIR`。

### 4.6 手动新增一个 Agent

当前部署没有单一的 Profile 注册表。新增 Agent 时，除了创建 `profiles/<profile>/`，还必须把它加入 bootstrap、凭据校验和验收脚本中的显式清单。只复制 Profile 目录会导致初始化脚本不创建 `.env`、容器不安装该 Profile，或运行态校验忽略它。

#### 4.6.1 先确定 Agent 类型和权限

先写清以下边界，并由人类审查确认后再创建 Profile：

| 决策 | 可选项 | 对配置的影响 |
| --- | --- | --- |
| 启动方式 | Kanban worker / 人类直接对话的 Gateway | Gateway 需要独立飞书 App、唯一端口和 lark-cli 状态目录 |
| GitLab 权限 | Reporter / Developer / Maintainer | 分别对应只读与评论、提交报告、调度或合并；不要默认授予 Maintainer |
| 是否写 Git | 否 / 是 | 写 Git 时需要独立 commit identity 和 Developer 以上角色 |
| 是否进入正式 SDD | 否 / 是 | 进入时必须同时修改 dispatcher 路由、卡片契约、阶段 schema 和测试 |
| 数据来源 | GitLab、飞书或其他明确来源 | 只启用任务需要的 toolsets、外部 Skills 和凭据 |

周报、月报或其他管理报告默认建议做成**独立的人类 Gateway Agent**，不插入 `PRD → code → merge` 主流程。它可以按明确时间范围读取允许的 GitLab 项目和飞书资料，生成报告并在原会话回复；默认使用 Reporter，只在确实要把报告提交到仓库时才升级为 Developer。

Profile 名必须使用小写英文和 `-`，例如 `report-writer`。该名称会同时用于目录名、`HERMES_PROFILE`、Hermes profile 名、Kanban assignee 和运行态路径，创建后不要随意改名。

#### 4.6.2 创建 Profile 骨架

Gateway 报告 Agent 可从权限最接近的 `fde` 复制；纯 Kanban worker 可从最接近的现有 worker 复制。复制只用于获得目录结构，必须重写身份和 Skill，不能继承原 Agent 的业务职责。

```bash
new_profile=report-writer
cp -R profiles/fde "profiles/${new_profile}"
mv "profiles/${new_profile}/skills/sdd-triage-field-input" \
  "profiles/${new_profile}/skills/write-periodic-report"
```

每个 Profile 必须保留以下结构，且当前静态校验要求恰好有一个入口 `SKILL.md`：

```text
profiles/report-writer/
├── .env.template
├── SOUL.md
├── config.yaml
├── distribution.yaml
├── profile.yaml
├── bootstrap/memories/MEMORY.md
├── bootstrap/memories/USER.md
└── skills/write-periodic-report/SKILL.md
```

逐项修改：

1. `distribution.yaml`：将 `name` 改为 `report-writer`，重写英文描述；`env_requires` 必须与 `.env.template` 的必需凭据一致。
2. `profile.yaml`：用一句话描述职责、输入、输出和明确禁区，保留 `description_auto: false`。
3. `SOUL.md`：定义报告范围、证据来源、统计口径、缺失数据处理、脱敏规则、输出语言以及禁止编造数据等边界。
4. `SKILL.md`：定义触发条件、必需输入、读取顺序、报告模板、引用要求、验收项和失败行为。周报/月报应明确时区、起止时间、项目范围，并区分“无数据”和“读取失败”。
5. bootstrap memory：只写稳定的角色约束，不预填虚构用户、项目或业务数据。运行中产生的 Memory 和 Skill 修改仍分别受 `memory.write_approval: true`、`skills.write_approval: true` 约束。
6. `config.yaml`：只启用必要工具，并完整复制 1.3.1 节统一的 `skills.disabled`，同时保留 `curator.prune_builtins: false`、fleet 的模型、`terminal.cwd: /workspace/projects`、`terminal.home_mode: profile`、`worktree: false`、中文显示和写入审批策略。独立 Gateway 设置 `kanban.dispatch_in_gateway: false`，避免它自行推进正式交付卡片。

人类在合入新 Agent 前必须审查 `SOUL.md`、`SKILL.md`、toolsets、GitLab 角色和所有新增凭据需求；Agent 不能自行扩大这些权限。

#### 4.6.3 配置 `.env.template`

纯 worker 只保留独立 GitLab 身份：

```dotenv
HERMES_PROFILE=report-writer
GITLAB_HOST=
GITLAB_ALLOWED_GROUPS=
GITLAB_TOKEN=
```

如果它是飞书 Gateway，还需增加与 `fde` 相同类型的飞书/Lark 变量，但使用自己的路径和未占用端口。当前 `8642`—`8644` 已使用，以下示例使用 `8645`；添加前仍应搜索所有模板确认没有冲突：

```dotenv
API_SERVER_PORT=8645
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_DOMAIN=feishu
FEISHU_CONNECTION_MODE=websocket
FEISHU_ALLOW_ALL_USERS=true
FEISHU_ALLOWED_USERS=
FEISHU_HOME_CHANNEL=
FEISHU_GROUP_POLICY=open
FEISHU_REQUIRE_MENTION=true
FEISHU_ALLOW_BOTS=mentions
LARKSUITE_CLI_CONFIG_DIR=/opt/data/profiles/report-writer/.lark-cli/config
LARKSUITE_CLI_DATA_DIR=/opt/data/profiles/report-writer/.lark-cli/data
LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1
LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1
```

必须为新 Profile 创建独立 `GITLAB_TOKEN`；Gateway 还必须创建独立飞书 App，不能复制已有 Token、App ID 或 App Secret。模型继续使用 fleet 的统一配置和 Codex OAuth，不在 Profile `.env` 中复制模型凭据。

#### 4.6.4 注册到所有运行清单

将新名称加入以下位置，Gateway 相关清单只在该 Agent 确实需要人类直接对话时添加：

| 位置 | 必改内容 |
| --- | --- |
| `scripts/container-bootstrap.sh` | `profiles`；Gateway 再加入 `gateway_profiles`；写 Git 再加入 commit identity 校验与配置逻辑 |
| `scripts/init-profile-envs.sh` | `profiles`，使初始化脚本能创建持久化 `.env` |
| `scripts/validate-profile-envs.py` | `PROFILES`、`EXPECTED_ACCESS_LEVELS`；Gateway 再加入 `GATEWAY_PROFILES` |
| `scripts/start-profile-gateways.sh` | 仅 Gateway：加入启动循环 |
| `scripts/verify-bundle.sh` | Shell/Python Profile 清单、Gateway 集合、数量和角色断言 |
| `scripts/verify-runtime.sh` | 容器内 Profile/Gateway 清单和动态数量提示 |
| `tests/test_profile_envs.py`、`tests/test_runtime_verifier.py` | 新 Profile fixture；Gateway 增加唯一端口；更新固定的 12/3/9 数量断言 |
| 本 README | Agent 职责、凭据角色、Profile/Gateway/worker 数量和运维说明 |

如果报告 Agent 要向仓库提交 Markdown 报告，还要：

1. 在根 `.env.example` 和实际 `.env` 增加 `GIT_COMMIT_NAME_REPORT_WRITER`、`GIT_COMMIT_EMAIL_REPORT_WRITER`。
2. 在 `docker-compose.yml` 将这两个非秘密变量传入容器。
3. 在 `container-bootstrap.sh` 为该 Profile 配置独立 git `user.name`、`user.email`。
4. 将 `EXPECTED_ACCESS_LEVELS["report-writer"]` 设为 Developer 的 `30`；只读报告 Agent 使用 Reporter 的 `20`。

如果新 Agent 要成为正式 SDD 的新阶段，不能只把名字加入 Profile 清单。还必须修改 dispatcher 的 Skill/路由规则、Kanban card 模板、completion schema、gate 失效规则、恢复逻辑和合同测试。独立周报/月报 Agent 不做这些改动。

可用以下搜索检查是否仍有旧的固定数量或漏改清单：

```bash
rg -n '12 个|12 profiles|九个 worker|3 个 Gateway|profiles=\(|PROFILES =|GATEWAY_PROFILES|EXPECTED_ACCESS_LEVELS|gateway_profiles=' \
  README.md scripts tests
```

#### 4.6.5 初始化、启动和验收

先执行静态校验，再创建新 Profile 的持久化凭据文件：

```bash
./scripts/verify-bundle.sh
./scripts/init-profile-envs.sh
```

填写 `${HERMES_DATA_DIR}/profiles/report-writer/.env`，保持目录 `0700`、文件 `0600` 和正确 owner，然后重启：

```bash
docker compose restart hermes
./scripts/verify-runtime.sh
```

最后进行最小权限 smoke test：

1. 确认 `hermes profile list` 中存在新 Profile；若为 Gateway，确认 Gateway running 且其他 worker 没有误启动 Gateway。
2. 用允许用户请求一个很短的指定日期范围报告，确认原渠道回复、中文格式、时区和无数据处理正确。
3. 验证它只能访问 `GITLAB_ALLOWED_GROUPS`，不能使用其他 Profile 的凭据。
4. Reporter 模式下验证它不能 push；Developer 模式下只向测试仓库/分支提交一次报告并检查 commit identity。
5. 尝试诱导它创建 SDD run、修改代码或扩大报告范围，确认 SOUL/Skill 权限边界生效。
6. 检查 Memory/Skill 新写入只进入 pending，未经过人类批准不会正式生效。

以上检查通过只能说明“新 Agent 的 Profile、凭据隔离和最小能力已验收”。在真实数据源、周期调度、报告接收人和失败通知均验证前，不声明该报告 Agent 已可生产自治运行。

### 4.7 备份与恢复

备份前停止 Hermes：

```bash
docker compose stop hermes
```

完整备份：

- 根 `.env`。
- 整个 `HERMES_DATA_DIR`，包括 Kanban SQLite、profiles、sessions、每个 Profile 的 `skills/`、`pending/skills/`、初始化标记和 lark-cli 状态。批准后的角色 Skill/Agent Skill 及待审批修改只存在于这条永久数据边界。
- 整个 `PROJECTS_DIR`，或确认所有未合并分支已安全 push 后再从 GitLab重建。
- `config/skills-lock.yaml` 对应的 tooling volume，或保证恢复环境可以重新联网同步。

恢复时必须保持文件所有者、`0700`/`0600` 权限和 Compose 项目/volume 名称一致。恢复后先执行静态校验，再启动并运行 `verify-runtime.sh`。

### 4.8 删除 tooling volume

```bash
docker compose down -v
```

该命令删除 named tooling volume，下次启动需要重新联网下载 CLI 和 Skills。它不会自动删除 bind-mounted 的 `HERMES_DATA_DIR` 或 `PROJECTS_DIR`。删除实际运行数据前必须另行备份并确认目标路径。

## 5. 常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `No such image` | 锁定 digest 不在本机，且 `pull_policy: never` | 手工准备 `.env.example` 中的完整 `tag@sha256` 后重试 |
| Dashboard s6 down | basic auth 未初始化、容器非 loopback bind 时 fail-closed | 在固定 checkout 重新运行 `init-deployment-env.sh`，确认根 `.env` 为 `0600` 后重启 |
| Dashboard status 未显示 basic | hash/signing secret 未注入或 Dashboard 未以 `0.0.0.0` 绑定 | 检查 Compose 三个 basic auth 变量，禁止改用明文密码或 `--insecure` |
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
| completion metadata 缺 `worktree`/`project_*` | worker 提交了阶段结果但未复制扁平 v2 公共上下文 | 同一卡修正完整 metadata 后重试；历史完成卡使用审计 `kanban edit` 回填或重跑，禁止 schema 例外 |
| 容器重启后 run 未推进 | Kanban card blocked/paused、凭据故障或 worker attempt 耗尽 | 查看 board/card 和 profile 日志，修复原因后通过 dispatcher 恢复 |

## 6. 验证声明边界

```bash
./scripts/verify-bundle.sh
```

验证仓库结构、Hermes 0.19 锁、profile 契约、凭据模板、schema、模板、脚本、合同测试和 Compose 配置。

```bash
./scripts/verify-runtime.sh
```

在 Ubuntu 启动后验证固定 bundle commit、镜像 digest、容器、Hermes/CLI 版本、profiles、Gateway、Memory 审批、Dashboard basic auth、凭据隔离和 GitLab 只读身份。

两者通过只能声明“静态部署包及本机只读运行验收通过”。第 3.10 节全部真实验收通过后才可声明“Go：内网受控试运行”；在 GitLab 写入、三个飞书机器人端到端消息、protected branch、MR/pipeline/discussion、checked-head merge、崩溃恢复和故障注入全部通过前，不得声明“生产自治 E2E”。
