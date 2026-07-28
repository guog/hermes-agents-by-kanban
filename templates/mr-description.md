# PRD 交付摘要

<本 PRD 的业务范围、实现摘要和中文项目显示名>

<!-- HOLLYSYS-RUN:BEGIN -->
```yaml
protocol_version: hollysys-controller/v2
run_key: <run_key>
project_id: <gitlab-project-id>
project_path: <group/project>
project_display_name: <display-name>
source_branch: <shared-run-branch>
target_branch: <gitlab-default-branch>
repository_base_sha: <run-start-default-branch-sha>
source_prd:
  path: docs/prds/<prd-basename>.md
  commit_sha: <merged-prd-commit-sha>
  blob_sha: <fixed-prd-blob-sha>
  mr_url: <merged-prd-mr-url>
artifacts:
  specs: [<sorted-spec-paths>]
  plans: [<sorted-plan-paths>]
  tasks: [<sorted-task-paths>]
gates:
  spec_disposition: <reviewed|forced_after_review_limit|null>
  spec_digest: <sha256-or-null>
  plan_disposition: <reviewed|forced_after_review_limit|null>
  plan_digest: <sha256-or-null>
  tasks_disposition: <reviewed|forced_after_review_limit|null>
  tasks_digest: <sha256-or-null>
```
<!-- HOLLYSYS-RUN:END -->

## 现有 MES 基线与定制方式

- 已检查路径：`<code/doc/config/test paths>`
- 现有能力：`<reused existing capabilities>`
- 变更类型：`extend_existing|modify_existing|extend_and_modify`
- 复用与改造摘要：`<what was reused, extended, and modified>`
- 新增结构及必要性：`<evidence or none>`

## 关键自主决策

`spec-writer` 首次创建本 MR 前填写。若没有关键决策，保留一行“无”；SPEC
后续迭代产生新的关键决策时更新同一表格，不另发首次决策评论。

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
