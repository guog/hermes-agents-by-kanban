# [<run_key>] <STAGE> <subject>

```yaml
schema_version: 2
identity:
  run_key: <sdd-base32-key>
  project_id: <gitlab-project-id>
  project_path: <group/project>
  project_display_name: <description-derived-chinese-name-or-project-path>
  stage: <controlled-stage>
  iteration: <positive-integer>
  idempotency_key: <stable-key>
  created_by: dispatcher
dispatch:
  card_role: <work-or-dispatcher-continuation>
  transition_key: <run_key>:<next-stage>:<iteration>
  awaits_parent_card_id: <worker-card-id-for-continuation-or-null>
  awaits_parent_stage: <worker-stage-for-continuation-or-null>
  live_reconcile_required: true
origin:
  parent_card_ids: [<card-id>]
  feishu:
    message_id: <om_xxx>
    chat_id: <oc_xxx>
    thread_id: <omt_xxx-or-null>
    initiator_open_id: <ou_xxx>
workspace:
  board: gitlab-p<project-id>
  checkout: /workspace/projects/p<project-id>-<repo-slug>
  worktree: /workspace/projects/worktrees/p<project-id>/<run_key>
  branch: <conventional-branch>
  target_branch: <gitlab-default-branch>
source:
  prd_path: docs/prds/<prd-basename>.md
  prd_commit_sha: <40-char-merged-commit-sha>
  prd_mr_url: <merged-prd-mr-url>
delivery:
  mr_iid: <number-or-null>
  mr_url: <url-or-null>
  expected_head_sha: <40-char-sha-or-null>
  artifact_paths: []
  artifact_digest: <sha256-or-null>
action:
  - <one role-owned action>
acceptance:
  - <objective completion rule>
output:
  schema: schemas/card-completion.schema.json
reconcile_before_write:
  - Parent completion metadata for dispatcher continuations
  - GitLab project, branch, MR, comments and current head
prohibitions:
  - never create a GitLab Task work item for formal delivery
  - never create a second delivery branch or MR for this run
  - never merge unless this is the checked-head dispatcher merge card
  - never expose credentials or raw sensitive logs
```

completion metadata 契约：

- `identity`、`workspace`、`source`、`delivery` 是卡片输入分段，不是 completion metadata 的输出结构。
- `kanban_complete(metadata=...)` 必须提交一个扁平的 v2 JSON 对象，并在顶层重复：
  `schema_version`、`run_key`、`stage`、`outcome`、`project_id`、`project_path`、
  `project_display_name`、`checkout`、`worktree`、`branch`、`target_branch`、
  `prd_path`、`prd_commit_sha`、`prd_mr_url`、`kanban_card_id`、`mr_iid`、
  `mr_url`、`head_sha`、`artifact_paths`、`artifact_digest`、`verification`、
  `residual_risk`。
- schema 允许未知值为 `null` 或 `[]` 时仍必须保留字段；不得用嵌套
  `workspace`/`project` 对象代替顶层字段。
- `spec-review`、`plan-review`、`tasks-review` 的 `pass` 还必须提交非空
  `artifact_paths`、`artifact_digest` 和 `review_commit_sha`；test/code-review/
  merge 的阶段字段按 schema 提交。
- 正式 SDD 卡在 Hermes 公共完成入口强制校验；失败时卡片保持 in-flight，
  修正同一 metadata 后重试，不得改为 block 或申请 schema 例外。
- 本卡若作出关键自主决策，completion metadata 的 `gitlab_urls` 必须包含
  对应 MR description 或幂等 MR comment URL；没有关键决策时不发布空评论。

执行协议：

1. `kanban_show()` 读取完整卡片，并拒绝 `created_by != dispatcher`、项目/分支/run 不一致的卡。
2. 进入卡片指定的共享 `worktree`；禁止自行猜仓库或新建 checkout。
3. GitLab 项目/MR/pipeline/discussion/comment 操作只使用锁定的 `glab` CLI 或卡片指定的官方 `glab` Skill；不得改用 raw HTTP/`curl`、临时 SDK、浏览器或手工 UI。Skill 明确要求的本地 `git` 检查、commit 和 push 仍可执行。
4. 该 `worktree` 是 Agent 所有交付分支在 Hermes 上唯一可编辑工作副本。dispatcher 初次准备/恢复后禁止阶段间例行 `git fetch origin` 或反复 pull；仅在缺少 ref/worktree、已有证据表明本地/远端 head 不一致、push 被拒绝或 `live_reconcile_required` 时 fetch，并记录原因。MR/pipeline/discussion live state 用 `glab` 查询，本地代码状态用 `git rev-parse`/`git status`。
5. PRD 未说明或表述模糊本身不是 `needs_input`/`scope_gap`。依次依据明确验收与约束、仓库现状与惯例、已批准上游工件、兼容性/安全性和最小可逆范围自主决策。影响用户可见范围/验收、公共接口、数据/迁移、安全/权限、兼容性、恢复/回滚或必需测试门禁的属于关键决策：交付 MR 尚不存在时将决策明确交给 `spec-writer`，由其首次创建 MR 时写入 description 的 `## 关键自主决策`；MR 已存在后，每张卡使用 `/opt/fleet/templates/decision-comment.md` 发布或更新一条包含本卡全部关键决策的幂等 comment，reviewer/tester 可合并到原 gate comment。只有证据互相冲突且不存在保持验收的安全选择，或确实缺少权限、凭据、能力时才请求人类。
6. 长任务定期 `kanban_heartbeat(note=...)`。
7. 详细产物和证据写入同一 Draft MR；Kanban 只保存状态、摘要与指针。
8. 正常完成调用 `kanban_complete(summary=..., metadata=<扁平完整v2对象>)`；无法完成调用 `kanban_block(kind=..., reason=...)`。
9. dispatcher gate 必须先创建/复用 `<transition_key>:work`，再创建/复用以该 worker 为父卡的 `<transition_key>:continue`；两卡和依赖均确认后才能完成当前 gate。
10. dispatcher continuation 只记录待读取的父卡，不预填未知 `head_sha`、digest、review/pipeline/merge 结论；被唤醒后同时重读父卡 completion metadata 与 GitLab live state。
11. completion schema 校验失败属于可重试的协议错误；修正同一卡的 metadata 后再次完成，禁止把评论、卡片正文或 schema 例外当作缺失字段的替代品。
