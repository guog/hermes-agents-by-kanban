# [<run_key>] <STAGE> <subject>

```yaml
schema_version: 1
identity:
  run_key: <run_key>
  project_id: <gitlab-project-id>
  project_path: <group/project>
  spec_key: <feature-key-or-null>
  stage: <controlled-stage>
  iteration: <positive-integer>
  idempotency_key: <stable-key>
origin:
  parent_task_ids: [<task-id>]
  feishu_message_id: <om_xxx-or-null>
inputs:
  prd_file: <path>
  prd_commit_sha: <40-char-sha>
  artifact_paths: []
  mr_iid: <number-or-null>
  expected_head_sha: <40-char-sha-or-null>
action:
  - <one role-owned action>
acceptance:
  - <objective completion rule>
output:
  schema: schemas/card-completion.schema.json
  required_extra_fields: []
reconcile_before_write:
  - <GitLab/Kanban object to query first>
prohibitions:
  - do not merge unless this is a dispatcher gate card
  - do not change requirements outside approved artifacts
  - do not expose credentials or raw sensitive logs
```

执行协议：

1. `kanban_show()` 读取完整上下文。
2. 进入 `$HERMES_KANBAN_WORKSPACE`。
3. 长任务定期 `kanban_heartbeat(note=...)`。
4. 完成动作后把详情写入 GitLab，Kanban 只保留摘要和指针。
5. 正常完成调用 `kanban_complete(summary=..., metadata=...)`；无法完成调用 `kanban_block(kind=..., reason=...)`。

