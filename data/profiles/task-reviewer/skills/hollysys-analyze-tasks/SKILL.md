---
name: hollysys-analyze-tasks
description: 当 Kanban 卡要求审查 TASKS 时，核对 SPEC/PLAN 映射、DAG、覆盖与验收并发布门禁。
version: 1.0.0
---

# 审查 TASKS 集合

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 明确要求的常规只读 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是准确工作副本。Controller 已在发卡前对账；不要例行 fetch/pull。只在缺少 ref 或已证实头不一致时 fetch，并记录原因。
- PRD 存在遗漏或歧义，本身不构成阻断性问题。应依次根据明确的验收条件/约束、当前仓库行为和约定、已批准的上游产物、兼容性/安全性作出判断，并选择最小且可逆的范围。若决策影响用户可见的范围/验收、公共接口、数据/迁移、安全/权限、兼容性、恢复/回滚或必需的测试/门禁，则属于关键决策。将每项关键决策写入幂等门禁 MR 评论的 `## 关键自主决策` 章节，并把该评论 URL 写入完成元数据的 `gitlab_urls`；如果没有关键决策，在已有门禁评论中填写 `无`，不要另外发布空评论。只有当证据互相矛盾且不存在能保留验收语义的安全选择，或确实缺少权限、凭据或能力时，才上报。
- 真正需要人类时，严格执行卡片中的“人类阻塞协议”：确认自己的原渠道订阅仍存在，写入幂等 `[human-block:v1]` 评论，再 `kanban_block`；不得自行发飞书、退订、unblock 或创建恢复卡。正常完成前退订当前卡。

1. 调用 `kanban_show()`；要求 `created_by=hollysys-controller`，并验证卡片 JSON 的 protocol/run/stage/iteration/assignee/parent、项目、worktree、分支、共享 MR 和已批准 SPEC/PLAN。
2. 读取 `/opt/fleet/templates/tasks-template.md`；验证一一对应的 SPEC/PLAN/TASK 键集合、严格的检查清单行、稳定且唯一的 ID、显式 `depends_on`、无环依赖、执行波次、需求覆盖、准确的目标路径、客观验收条件，以及足够且可重复执行的测试工作。报告实质性执行缺陷，不报告纯样式差异。
3. 在审查 commit 上计算完整且排序后的 TASKS path/blob-SHA 摘要。发布一条包含完整路径集合、摘要、`review_commit_sha` 和当前 card ID 的幂等 v3 `tasks-review` 评论；Controller 会独立重算。
4. 任务缺失或错误时使用 `fail`。只有存在证据表明 PLAN 或 SPEC 有缺陷时才使用 `scope_gap`，并指出负责的上游阶段。
5. 完成时提交严格 v3 metadata，必含 `protocol_version=hollysys-controller/v1`、卡片 `iteration` 和完整上下文。pass 必须包含非空路径、digest、review commit；scope_gap 必须给目标与问题。必须通过仓库 Schema，不得包含下一卡字段。绝不得编辑产物、实现、push、创建卡片/MR 或合并。
