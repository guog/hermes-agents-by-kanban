# PRD 交付摘要

<本 PRD 的业务范围、实现摘要和中文项目显示名>

<!-- SDD-RUN:BEGIN -->
```yaml
schema_version: 2
run_key: <run_key>
project_id: <gitlab-project-id>
project_path: <group/project>
project_display_name: <display-name>
source_branch: <shared-run-branch>
target_branch: <gitlab-default-branch>
source_prd:
  path: docs/prds/<prd-basename>.md
  commit_sha: <merged-prd-commit-sha>
  mr_url: <merged-prd-mr-url>
artifacts:
  specs: [<sorted-spec-paths>]
  plans: [<sorted-plan-paths>]
  tasks: [<sorted-task-paths>]
gates:
  spec_digest: <sha256-or-null>
  plan_digest: <sha256-or-null>
  tasks_digest: <sha256-or-null>
```
<!-- SDD-RUN:END -->

## 关键自主决策

`spec-writer` 首次创建本 MR 前填写。若没有关键决策，保留一行“无”；SPEC
返工产生新的关键决策时更新同一表格，不另发首次决策评论。

| 决策 ID | PRD 未明确或模糊点 | 自主决策 | 依据 | 影响与可逆方式 |
| --- | --- | --- | --- | --- |
| `<stable-decision-id-or-none>` | <ambiguity-or-none> | <decision-or-none> | <acceptance/repository/upstream/security evidence> | <scope/compatibility/rollback> |

## TASKS 与实现覆盖

| TASK ID | SPEC/需求 | 实现位置 | 测试 |
| --- | --- | --- | --- |
| <id> | <source> | <path> | <test> |

## 验证

- `<command>`：<result>

## 风险与回滚

- 风险：<risk-or-none>
- 回滚：<rollback>

## 合并后永久链接评论

dispatcher 合并成功后，以 GitLab 返回的 `merge_commit_sha` 为 revision，逐项生成 PRD、全部 SPEC、PLAN、TASKS 的 `/-/blob/<merge_commit_sha>/<path>` 链接，并在本 MR 留下一个幂等评论；不得使用分支名或合并前 head 代替 merge SHA。
