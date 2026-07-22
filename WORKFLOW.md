# Hermes 单 PRD 单 MR 自动交付流程

本文是本部署包的权威流程合同。README 只说明部署和日常操作；profile Skill、模板与 schema 必须与本文一致。

## 1. 边界与运行对象

人类只在飞书启动一次，Hermes Kanban 持久调度：

```text
PRD → SPEC 全集 → SPEC review → PLAN 全集 → PLAN review
    → TASKS 全集 → TASK review → code → test → code review
    → checked-head merge
```

一个已合入 PRD 版本只对应一个 `run_key`、一个共享分支、一个共享 worktree 和一个 Draft MR。正式交付不创建 GitLab Task work item；Hermes Kanban card 是运行对象，GitLab MR 是工件、评论、pipeline 与门禁证据面。`TASK` 仅指仓库中的 SDD TASKS 文档。

正常路径无需人类逐段唤醒。只有需求冲突、权限/凭据/环境故障或返工预算耗尽时暂停。

## 2. Agent 与权限

单容器保留 12 个 profile：

| Profile | 职责 | GitLab 最低角色 | Gateway |
| --- | --- | --- | --- |
| `dispatcher` | 校验入口、构造/恢复 DAG、核对门禁、checked-head merge、飞书通知 | Maintainer，能合并 protected branch | 独立飞书 Gateway；唯一 Kanban dispatcher |
| `prd-writer` | 与人类编写 PRD、创建 PRD MR，不启动正式 run | Developer | 独立飞书 Gateway |
| `fde` | 整理现场反馈、脱敏、创建普通 Issue | Reporter + API | 独立飞书 Gateway |
| `spec-writer`、`planner`、`tasker`、`coder` | 在共享分支提交各自工件或代码 | Developer | 无 |
| `spec-reviewer`、`plan-reviewer`、`task-reviewer`、`tester`、`code-reviewer` | 读取仓库并在共享 MR 留门禁证据 | Reporter + API | 无 |

FDE 不写 Git，因此不配置 commit identity。每个 profile 使用独立群组访问令牌；不得用升权共享令牌掩盖权限错误。

## 3. 飞书入口与原渠道回复

正式启动协议只有：

```text
实现 PRD <精确 PRD blob/raw URL> <已合并 PRD MR URL>
```

缺少任一 URL、两个 URL 不属于同一 `project_id`，或不能唯一确定仓库时，dispatcher 在原渠道继续询问且不创建 run。消息来源可能是单聊、群聊或话题；所有状态和结果都回到原 `chat_id`/`thread_id`，群聊和话题必须 `@` 原发起人，单聊直接回复。

dispatcher 按顺序执行只读校验：

1. URL host 与项目 `path_with_namespace` 位于 `GITLAB_HOST`/`GITLAB_ALLOWED_GROUPS`。
2. 项目 API 可读取。项目不存在与无权访问不可可靠区分时，统一如实提示“项目不存在或无访问权限”。
3. 只有项目已确认存在后，PRD 路径缺失才提示“文件不存在”。
4. `archived=false`；已归档立即停止。
5. PRD MR 已 merged，目标是项目当前 `default_branch`，且 URL 所指 `docs/prds/prd-*.md` 位于该 MR 的有效合入版本。
6. 默认分支当前的 PRD 内容仍对应此合入版本；旧版本不得启动。

项目描述中的中文名称仅用于消息展示。所有身份判断、目录和幂等键使用不可变 `project_id` 与 `path_with_namespace`；仓库名即使只是中文名称的拼音首字母也不得用于猜测身份。

## 4. run 身份、恢复与工作区

```text
run_key = "sdd-" + base32(sha256(host | project_id | prd_path | prd_commit_sha))[0:20]
```

同一 `run_key` 已存在时：

- 活跃或暂停的 run：恢复 Kanban/GitLab live state并返回当前状态，不创建第二个 run、分支或 MR。
- 交付 MR 已 merged：返回既有完成结果。
- 同一路径的新 PRD 合入 commit：得到新 `run_key`，可以启动新 run。

dispatcher 只用 token-free HTTPS origin 自动准备工作区：

```text
checkout  /workspace/projects/p<project_id>-<repo-slug>
board     gitlab-p<project_id>
worktree  /workspace/projects/worktrees/p<project_id>/<run_key>
```

`scripts/prepare-run-workspace.sh` 幂等校验 host、群组、origin、base commit 与既有 worktree。默认分支来自 GitLab `default_branch`，新分支的 base 是启动校验时该默认分支的当前 HEAD，而不是较旧的 PRD 合入点。分支优先服从目标仓库已有约定；没有可识别约定时使用：

```text
feature/<prd-basename>-<prd_sha8>
```

远端 URL 不嵌入 token。所有 worker card 必须完整携带：项目 ID/路径/中文显示名、checkout/worktree、共享分支、目标分支、PRD 路径与合入 commit、源 PRD MR、`run_key`、交付 MR、期望 head 和 `created_by=dispatcher`。worker 发现任何身份字段不一致时必须阻塞，不能自行寻找“可能的仓库”。

## 5. 工件目录与提交

不改名或迁移仓库已有文档。新工件固定为：

```text
docs/prds/prd-xxx-yyy.md
docs/prds/prd-xxx-yyy/specs/spec-<key>.md
docs/prds/prd-xxx-yyy/plans/plan-<key>.md
docs/prds/prd-xxx-yyy/tasks/task-<key>.md
```

`<key>` 使用小写语义化英文 kebab-case；SPEC、PLAN、TASK 文件一一对应。除传统 `.NET service` 目录沿用已有规则外，新文件和目录使用小写英文与连字符。

producer 在同一共享分支工作，按最小可验证单元使用约定式提交；可多次 commit。spec-writer 在首个有效 SPEC commit push 后创建且只创建：

```text
Draft: [PRD] prd-xxx-yyy.md
```

后续 producer 更新同一 MR。coder 完成全部 TASKS、配套测试和自测后将 MR 标为 ready；不能新建“代码 MR”。

## 6. 串行阶段与职责

1. `spec-writer` 一次生成完整、有序的 SPEC 集与 PRD 覆盖矩阵；只定义做什么。
2. `spec-reviewer` 在同一 MR 独立审查完整 SPEC 集。失败回到 spec-writer。
3. `planner` 为每个 `spec-<key>.md` 生成一个 `plan-<key>.md`；基于真实仓库定义实现方案。`plan-reviewer` 整批审查，失败回到 planner；上游范围缺口回到 SPEC。
4. `tasker` 为每个 SPEC/PLAN 对生成一个 `task-<key>.md`，包含稳定 ID、来源、模块、依赖、验收与测试。`task-reviewer` 整批审查；任务问题回到 tasker，上游缺口按证据回到 PLAN 或 SPEC。
5. `coder` 按全局依赖顺序实现全部 TASKS 并自测。设计不足必须报告 `scope_gap`，不得扩需求。
6. `tester` 对当前 MR 精确 `head_sha` 独立测试并评论；失败回到 coder。
7. `code-reviewer` 在测试后审查同一 `head_sha` 并评论；失败回到 coder。
8. dispatcher 重新读取 live state，执行 checked-head merge。

设计阶段每一阶段最多返工 3 轮；代码、测试或代码评审引发的代码返工合计最多 5 轮。预算耗尽转 `human-decision`。

## 7. 工件门禁与失效规则

SPEC/PLAN/TASKS 门禁不绑定整个 MR head，而绑定：

- 按字节序排序的阶段工件路径；
- 每个路径在审查 commit 的 Git blob SHA；
- 对 `<path>\0<blob_sha>\n` 清单计算的 SHA-256 `artifact_digest`；
- `review_commit_sha` 与 reviewer GitLab 身份。

因此新增 PLAN 或代码不会使已批准 SPEC 失效。重放时 dispatcher 重新计算当前对应路径清单：

| 变化 | 作废门禁 |
| --- | --- |
| SPEC 集新增、删除、改名或内容变化 | SPEC 及全部下游 |
| PLAN 集新增、删除、改名或内容变化 | PLAN、TASKS、test、code-review |
| TASKS 集新增、删除、改名或内容变化 | TASKS、test、code-review |
| 任意新 push 造成 MR head 变化 | tester 与 code-reviewer 两个代码门禁 |

tester 和 code-reviewer 的 pass 必须分别由允许身份发布，并同时绑定当前完全相同的 MR `head_sha`。后来任何 push 都同时作废二者，无论改动看似只涉及评论修复还是测试文件。

门禁评论采用 `templates/gate-comment.md` 的 v2 结构。相同 run/stage/baseline 的写入必须先查询再复用；同一 baseline 出现 fail/scope_gap 时不得被旧 pass 覆盖。

## 8. 合并、永久链接与清理

dispatcher 仅在以下条件同时成立时合并：

- MR 已 ready、GitLab 判断可合并；
- 所有 required pipeline 成功；
- 所有阻塞 discussion 已解决；
- 当前 SPEC/PLAN/TASKS digest 均与批准门禁匹配；
- tester 与 code-reviewer 对相同当前 `checked_head` 通过。

合并调用必须携带 `sha=<checked_head>`。若 GitLab 报 SHA 漂移，禁止改用新 SHA 重试，必须重新 test 和 code review。

合并成功后，dispatcher 使用 GitLab 返回的 `merge_commit_sha` 生成 PRD、全部 SPEC、PLAN、TASKS 的永久 blob 链接，在同一 MR 留幂等最终评论；再执行 `scripts/cleanup-run-worktree.sh` 清理成功 run worktree。分支删除服从项目策略。最后完成 Kanban run，并按原飞书渠道通知发起人。

## 9. Hermes 0.19.0 与部署约束

部署使用 Hermes Agent 0.19.0：源码 revision `3ef6bbd201263d354fd83ec55b3c306ded2eb72a`，镜像 tag `nousresearch/hermes-agent:v2026.7.20`，配置版本仍为 33。利用 0.19.0 的多 profile s6 Gateway 管理和 Feishu 修复，不改配置格式。

部署不构建派生镜像。Compose 直接使用已存在于宿主机的官方镜像 tag，并设置 `pull_policy: never`；profiles、模板、schema、配置、脚本和补丁从部署目录只读挂载。`tooling-sync` 也使用同一官方镜像，将锁定版本的 `glab`、`lark-cli` 与官方 Skills 安装到独立 Docker named volume；Hermes 容器只读挂载该卷。更新外部 CLI 或 Skills 只需修改 `config/skills-lock.yaml` 并重新同步工具卷，不需要构建 Hermes 镜像。

上游 0.19.0 仍允许 worker handler 到达 `kanban_create/link`。容器每次启动时，s6 cont-init 先运行 `scripts/runtime-patch-hermes.sh`：严格验证 Hermes 版本为 0.19.0，再将 `patches/hermes-0.19.0-dispatcher-kanban-guard.patch` 幂等应用到容器可写层。版本不匹配、目标不可写或补丁不适用时启动失败，不允许无保护运行。非 `HERMES_PROFILE=dispatcher` 一律拒绝 `kanban_create/link`；dispatcher 在放行下一卡前仍须检查卡片 `created_by=dispatcher`。只有 dispatcher config 设置 `kanban.dispatch_in_gateway=true`。

bootstrap 为 12 个 profile 写独立 `.env`，其中固定写入不可由卡片伪造的 `HERMES_PROFILE` 身份；三个 Gateway 还必须使用互不相同的 `API_SERVER_PORT`。bootstrap 随后清空 s6 容器全局凭据环境；s6 启动 dispatcher、prd-writer、fde 三个 Gateway，Compose 主进程只常驻等待。Hermes 原生 Gateway 是唯一飞书入站 consumer，不得并行运行 `lark-cli event consume`。

## 10. 验证与声明边界

`./scripts/verify-bundle.sh` 只证明官方镜像零构建合同、版本锁、挂载配置、工具卷同步脚本、schema、模板、权限补丁和静态合同一致。生产前还必须完成：

- 入口错配、旧 PRD、越界群组、归档项目、动态 clone、重复消息与恢复合同测试；
- 多 SPEC 整批门禁、digest 失效、三类设计返工、代码返工、head 漂移和永久链接测试；
- 三机器人原渠道回复、最小 GitLab 权限、protected branch、pipeline/discussion、checked-head merge；
- Gateway/worker 崩溃、容器重启、Kanban SQLite 恢复与故障注入。

完整 L2/L3 与故障注入通过前，只能声明“静态部署包通过”，不能声明“生产自治 E2E”。
