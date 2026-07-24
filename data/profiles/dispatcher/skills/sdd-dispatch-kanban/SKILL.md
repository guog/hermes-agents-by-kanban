---
name: sdd-dispatch-kanban
description: 在白名单 GitLab 项目中启动、恢复并合并单 PRD 单 MR 的交付运行
version: 0.3.0
---

# SDD Kanban 调度器

本 Skill 仅用于正式运行的受理、确定性的阶段转换、恢复、基于已检查头提交的合并、状态查询和原渠道通知。绝不得编写专业的 SPEC/PLAN/TASKS/代码或其审查内容。

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论、评论和合并操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 为准备和检查 checkout/worktree、commit 和 push 而明确要求的常规 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是 Agent 所属交付分支唯一可编辑的工作副本。完成首次准备/恢复后，不要在阶段之间例行运行 `git fetch origin` 或循环拉取。只有在创建/恢复缺失的 ref 或 worktree、调查已证实的本地/远端头提交不一致或 push 被拒绝，或满足 `live_reconcile_required` 时才能 fetch，并记录原因。使用本地 `git rev-parse`/`git status` 判断 worktree 状态，使用 `glab` 获取当前 MR、流水线和讨论状态。
- PRD 存在遗漏或歧义，本身不等于 `needs_input`。应依次根据明确的验收条件/约束、当前仓库行为和约定、已批准的上游产物、兼容性/安全性作出判断，并选择最小且可逆的范围。若决策影响用户可见的范围/验收、公共接口、数据/迁移、安全/权限、兼容性、恢复/回滚或必需的测试/门禁，则属于关键决策。如果交付 MR 已存在，使用 `/opt/fleet/templates/decision-comment.md` 对账一条幂等 MR 评论，其中包含本卡片作出的全部关键决策，并将其 URL 写入完成元数据的 `gitlab_urls`。首个 MR 创建前，要将这些决策明确写入 spec-writer 卡片，使其能够放入初始 MR 描述。若没有关键决策，不要发布空评论。只有当证据互相矛盾且不存在能保留验收语义的安全选择，或确实缺少权限、凭据或能力时，才询问人类。

## 受理

1. 只接受 `实现 PRD <exact blob/raw URL> <merged PRD MR URL>`。该命令中的两个英文占位符属于固定协议，不翻译。如果缺少任一 URL、两者的 host/project ID 不一致，或仓库身份不明确，则只在原飞书渠道询问缺失/正确的值，不要创建卡片。
2. 按顺序查询 GitLab 并确认：host/group 在白名单中；项目可读；PRD 路径存在；`archived=false`；MR 已合并到当前 `default_branch`；该合并版本包含 PRD；当前默认分支上的 PRD 仍等于该生效版本。不得把不可读的项目称为“文件不存在”。
3. 使用 `project_id` 和 `path_with_namespace` 作为身份标识。项目描述中的中文文本仅用于显示。
4. 推导 `run_key = sdd-<base32(sha256(host|project_id|prd_path|prd_commit_sha))[0:20]>`。创建任何内容前查询 Kanban 和 GitLab：恢复活动中的运行、返回已合并结果，或者只为新的 PRD commit 启动运行。
5. 读取当前 `default_branch` 的 HEAD 作为运行基准 SHA。优先选择仓库的分支约定；否则使用 `feature/<prd-basename>-<prd_sha8>`。推导：
   - checkout: `/workspace/projects/p<project_id>-<repo-slug>`
   - worktree: `/workspace/projects/worktrees/p<project_id>/<run_key>`
   - board: `gitlab-p<project_id>`
6. 使用标准 `git` 和 `hermes` 命令准备工作区，不使用舰队辅助脚本：
   - 要求项目 ID 为正整数，repo slug 为小写且适合文件系统，`run_key` 有效，基准 SHA 为完整小写形式，且分支能通过 `git check-ref-format --branch`。
   - 要求不含 token 的 clone URL 和任何已有 `origin` 都等于已验证的 `https://<GITLAB_HOST>/<path_with_namespace>[.git]`；要求项目始终位于 `GITLAB_ALLOWED_GROUPS` 内。
   - 创建父目录。只有 checkout 不存在时才 clone。只有首次准备、ref 缺失、已证实发生分歧、push 被拒绝或明确要求实时对账时才 fetch。
   - 验证 `base_sha^{commit}`。只有现有 worktree 的分支准确等于预期分支时才复用；否则依次从已有本地分支、已跟踪的远端分支或基准 SHA 创建。
   - 使用 `hermes -p dispatcher kanban boards create <board>` 创建或对账看板，并将项目显示名称和 checkout 分别用作看板名称信息和默认 workdir。
7. 使用稳定的消息/运行幂等键、`tenant=run_key`、`created_by=dispatcher`、准确的 assignee/skills 和 v2 卡片模板创建一张 `run-init` 卡片。保留 `message_id`、`chat_id`、`thread_id` 和发起人信息，以便恢复。

## 卡片和门禁对账

1. 调用 `kanban_show()`；依据 `/opt/fleet/schemas/card-completion.schema.json` 验证唯一且扁平的完成元数据对象。卡片正文的 `identity`/`workspace`/`source`/`delivery` 章节和评论不能补齐缺失的完成字段。若下一张卡片存储或返回的 `created_by` 不准确等于 `dispatcher`，则拒绝该卡片。
2. 每张卡片都必须重复项目 ID/path/显示名称、checkout/worktree、共享分支/目标分支、PRD path/commit/MR、运行、交付 MR 和预期头提交。每次写入前都要对账 GitLab 实时状态。
3. 只能通过下述“工作卡/续接卡成对协议”推进。舰队策略把 `kanban_create` 和 `kanban_link` 保留给 dispatcher，并禁止委派图结构塑造。未经修改的官方 Hermes 运行时并未通过打补丁的 handler 强制执行此策略，因此信任卡片前始终重新检查 `created_by=dispatcher`。
4. 产物门禁使用排序后的 path/blob-SHA 摘要、审查者身份和 `review_commit_sha`。每次转换时重新计算本阶段的路径集合。已批准产物发生变化会使该门禁及所有下游门禁失效；之后新增 PLAN/代码不会使内容未变的 SPEC 摘要失效。
5. `fail` 返回负责的生产者。`scope_gap` 按证据路由到 TASKS、PLAN 或 SPEC；绝不允许 coder 扩大范围。设计返工每阶段最多 3 次；代码返工总计最多 5 次。
6. tester 和 code-reviewer 门禁必须由允许的身份分别发布为独立评论，并绑定同一个当前 MR `head_sha`。任何 push 都会使两者失效。

### 工作卡/续接卡成对协议

每个 dispatcher 门禁（包括 `run-init` 和每张恢复执行的 dispatcher 续接卡）都必须在完成前创建一组逻辑卡片对：

1. 推导 `transition_key = <run_key>:<next-stage>:<iteration>`。按准确的 `idempotency_key` 对账卡片；绝不根据标题文本推断身份。
2. 创建或复用工作卡 `W`，其键为 `<transition_key>:work`，stage/assignee/Skills 必须准确，`created_by=dispatcher`，且 parent 等于当前 dispatcher 门禁。
3. 创建或复用 dispatcher 续接卡 `C`，其键为 `<transition_key>:continue`，assignee 为 `dispatcher`，且 parent 等于 `W`。`C` 只记录预期的父卡片/阶段和 `live_reconcile_required=true`。
4. 验证两张卡片及两条父子关系。只有完成验证后，才能使用 `next_card_ids=[W,C]` 完成当前门禁。如果任一卡片创建/链接失败，保持当前门禁为 running 或 blocked，并使用相同键重试；不得完成门禁。
5. 完成当前门禁只会将 `W` 提升为 `ready`。完成 `W` 会将 `C` 提升为 `ready`。`C` 必须读取 `W` 的完成元数据，依据 schema 验证，然后重新读取 GitLab 的项目、分支、MR、评论、流水线、讨论和当前头提交，之后才能决定下一组卡片对。对于历史遗留的无效交接，不创建任何内容，只接受经过审计的 `kanban edit` 回填或新的审查/工作卡片对；绝不得推断缺失字段或批准 schema 例外。
6. 绝不得把预测的 `head_sha`、`artifact_digest`、审查结果、流水线结果或合并结果写入 `C`。未知的实时事实在 `C` 运行前保持 null。重复消息、dispatcher 重启和 worker 重试必须复用同一组卡片对。

每次转换都使用同一协议：编写、独立审查、任何设计/代码返工、测试、代码审查和合并。合并工作卡分配给 `dispatcher`，其后接一张 `run-complete` 续接卡；只有基于已检查头提交的合并卡完成后，`run-complete` 才会变为 ready。

## 单 MR 生命周期和合并

1. spec-writer 在首次有效 SPEC commit 后创建且只创建一个 `Draft: [PRD] <prd-basename>.md` MR。此后的每个生产者都更新该分支/MR；每个 reviewer/tester 都在该 MR 中评论。coder 在实现和自测完成后将其标记为 ready。
2. 合并前要求：MR 已 ready、可以合并、必需的流水线成功、阻断性讨论已解决、当前产物摘要已批准，并且 tester/code-reviewer 在同一个 `checked_head` 上通过。
3. 合并工作卡及其完成元数据必须包含 `head_sha=<checked_head>` 和 `checked_head=<checked_head>`。通过 GitLab 使用 `sha=<checked_head>` 合并。如果 SHA 不匹配，则创建新的测试/审查卡片对；绝不得使用未经检查的 SHA 重试。
4. 使用返回的 `merge_commit_sha` 发布一条幂等 MR 评论，其中包含永久的 PRD/SPEC/PLAN/TASKS blob 链接。
5. 不使用舰队辅助脚本进行清理：只能根据已验证的项目 ID、repo slug 和运行键重新推导 checkout/worktree；要求 checkout 仍是预期的 Git 仓库；要求交付 MR 已合并且 worktree 没有未提交变更；然后依次运行 `git -C <checkout> worktree remove <worktree>` 和 `git -C <checkout> worktree prune`。绝不得递归删除推导出的路径。
6. 完成运行并通知原渠道/话题；在群聊/话题回复中 @ 发起人。

创建的每张卡片都要设置准确的 assignee 和 Skill 列表：

| 承担者（Assignee） | Skills |
| --- | --- |
| dispatcher | `sdd-dispatch-kanban`, `glab`, `lark-shared`, `lark-im` |
| spec-writer | `sdd-write-spec`, `glab` |
| spec-reviewer | `sdd-review-spec`, `glab` |
| planner | `sdd-write-plan`, `glab` |
| plan-reviewer | `sdd-review-plan`, `glab` |
| tasker | `sdd-create-tasks`, `glab` |
| task-reviewer | `sdd-analyze-tasks`, `glab` |
| coder | `sdd-implement`, `glab` |
| tester | `sdd-test`, `glab` |
| code-reviewer | `sdd-review-code`, `glab` |

只有在缺少必需的受理身份、需求互相矛盾且不存在能保留验收语义的安全选择、发生 `capability`/凭据/环境故障，或预算耗尽时，才能阻断流程，并给出明确的人类操作。仅有 PRD 歧义不能作为阻断理由。正式交付中绝不得创建 GitLab Task work item，也绝不得运行第二个飞书入站消费者。
