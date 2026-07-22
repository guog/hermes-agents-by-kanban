# 变更摘要

<由产出者填写对人类可读的摘要>

<!-- SDD-RUN:BEGIN -->
```yaml
schema_version: 1
run_key: <run_key>
kanban_task_id: <task-id>
project_path: <group/project>
artifact_type: <spec|plan|tasks|code>
spec_key: <feature-key>
source_branch: <branch>
target_branch: <protected-branch>
upstream:
  prd_file: <path>
  prd_commit_sha: <sha>
  spec_file: <path-or-null>
  spec_commit_sha: <sha-or-null>
  plan_file: <path-or-null>
  plan_commit_sha: <sha-or-null>
  tasks_file: <path-or-null>
  tasks_commit_sha: <sha-or-null>
```
<!-- SDD-RUN:END -->

## 验证

- `<command>`：<result>

## 风险与回滚

- 风险：<risk-or-none>
- 回滚：<rollback>

## 待确认

- 无；或列出会阻塞本 MR 的明确事项。

