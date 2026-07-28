---
name: hollysys-write-plan
description: 当 SPEC 已批准且 Kanban 卡要求规划时，在唯一共享 MR 编写可验证的完整 PLAN 集。
version: 1.0.0
---

# 编写 PLAN 集合

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 为检查 worktree、commit 和 push 而明确要求的常规 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是唯一可编辑副本。Controller 已在发卡前对账；不要例行 fetch/pull。只有缺少 ref、已证实头不一致或 push 被拒绝时才能 fetch，并记录原因。
- PRD 存在遗漏或歧义，本身不等于 `scope_gap` 或 `needs_input`。应依次根据明确的验收条件/约束、当前仓库行为和约定、已批准的上游产物、兼容性/安全性作出判断，并选择最小且可逆的范围。若决策影响用户可见的范围/验收、公共接口、数据/迁移、安全/权限、兼容性、恢复/回滚或必需的测试/门禁，则属于关键决策。完成前，使用 `/opt/fleet/templates/decision-comment.md` 对账一条幂等的交付 MR 评论，其中包含本卡片作出的每项关键决策，并将其 URL 写入完成元数据的 `gitlab_urls`；若没有关键决策，不要发布空评论。非关键假设记录在 PLAN 或完成证据中。只有当证据互相矛盾且不存在能保留验收语义的安全选择，或确实缺少权限、凭据或能力时，才上报。
- 真正需要人类时，严格执行卡片中的“人类阻塞协议”：确认自己的原渠道订阅仍存在，写入幂等 `[human-block:v1]` 评论，再 `kanban_block`；不得自行发飞书、退订、unblock 或创建恢复卡。正常完成前退订当前卡。

1. 调用 `kanban_show()`；要求 `created_by=hollysys-controller`，并验证卡片 JSON 的 protocol/run/stage/iteration/assignee/parent、项目、worktree/分支、PRD、交付 MR 和已批准 SPEC。
2. 阅读仓库规则、架构和相关代码。对于每个 `spec-<key>.md`，基于中文模板 `/opt/fleet/templates/plan-template.md` 创建且只创建对应的 `docs/prds/<prd-basename>/plans/plan-<key>.md`；替换所有占位符，不要重命名已有文档。
3. 保留模板中必需的技术上下文、治理检查、决策、架构、接口、数据/迁移、兼容性、可观测性、安全性、测试、回滚、真实项目结构、可追溯性和风险章节。将决策追溯到 SPEC 需求，但不得改变意图或定义最终 Task ID。
4. 返工时保留一一对应的键，只修改受影响的 PLAN。按上述层级解决缺失的设计决策；只有已批准需求互相矛盾，或无法支持任何能保留验收语义的安全计划时，才报告 `scope_gap`。
5. commit 最小且连贯的 PLAN 变更，并 push 到同一分支/MR。绝不得创建 PLAN MR。
6. 完成时提交严格 v3 metadata：必含 `protocol_version=hollysys-controller/v1`、卡片 `iteration`、完整上下文和验证证据；scope_gap 必须给目标与问题。必须通过仓库 Schema，不得包含 continuation/下一卡字段。不得编写 TASKS/代码、创建卡片、执行审查或合并。
