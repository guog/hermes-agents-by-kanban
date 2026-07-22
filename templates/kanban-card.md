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
  - GitLab project, branch, MR, comments and current head
prohibitions:
  - never create a GitLab Task work item for formal delivery
  - never create a second delivery branch or MR for this run
  - never merge unless this is the checked-head dispatcher merge card
  - never expose credentials or raw sensitive logs
```

执行协议：

1. `kanban_show()` 读取完整卡片，并拒绝 `created_by != dispatcher`、项目/分支/run 不一致的卡。
2. 进入卡片指定的共享 `worktree`；禁止自行猜仓库或新建 checkout。
3. 每次写入前查询 GitLab live state；长任务定期 `kanban_heartbeat(note=...)`。
4. 详细产物和证据写入同一 Draft MR；Kanban 只保存状态、摘要与指针。
5. 正常完成调用 `kanban_complete(summary=..., metadata=...)`；无法完成调用 `kanban_block(kind=..., reason=...)`。
