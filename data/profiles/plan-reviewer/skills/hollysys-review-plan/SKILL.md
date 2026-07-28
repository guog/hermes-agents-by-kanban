---
name: hollysys-review-plan
description: 当 Kanban 卡要求审查 PLAN 时，核对 SPEC、仓库约束与可执行性并发布门禁。
version: 1.0.0
---

# 审查 PLAN 集合

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 明确要求的常规只读 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是准确工作副本。Controller 已在发卡前对账；不要例行 fetch/pull。只在缺少 ref 或已证实头不一致时 fetch，并记录原因。
- PRD 存在遗漏或歧义，本身不构成阻断性问题。应依次根据明确的验收条件/约束、当前仓库行为和约定、已批准的上游产物、兼容性/安全性作出判断，并选择最小且可逆的范围。若决策影响用户可见的范围/验收、公共接口、数据/迁移、安全/权限、兼容性、恢复/回滚或必需的测试/门禁，则属于关键决策。将每项关键决策写入幂等门禁 MR 评论的 `## 关键自主决策` 章节，并把该评论 URL 写入完成元数据的 `gitlab_urls`；如果没有关键决策，在已有门禁评论中填写 `无`，不要另外发布空评论。只有当证据互相矛盾且不存在能保留验收语义的安全选择，或确实缺少权限、凭据或能力时，才上报。
- 真正需要人类时，严格执行卡片中的“人类阻塞协议”：确认自己的原渠道订阅仍存在，写入幂等 `[human-block:v1]` 评论，再 `kanban_block`；不得自行发飞书、退订、unblock 或创建恢复卡。正常完成前退订当前卡。

1. 调用 `kanban_show()`；要求 `created_by=hollysys-controller`，并验证卡片 JSON 的 protocol/run/stage/iteration/assignee/parent、项目、worktree、分支、共享 MR 和已批准 SPEC。
2. 为每个 SPEC 键枚举一个 `plan-<key>.md`。读取 `/opt/fleet/templates/plan-template.md`；检查其中实质性的必需章节、SPEC 可追溯性、相对于实际代码的可行性、架构适配、接口、数据/迁移、兼容性、安全性、可观测性、测试、回滚、未解决决策和过度设计。报告实质性遗漏，不报告纯样式差异。
3. 在审查 commit 上计算排序后的 path/blob-SHA 摘要。发布一条包含完整路径集合、摘要、`review_commit_sha` 和当前 card ID 的幂等 v3 `plan-review` 评论；Controller 会独立重算。
4. PLAN 缺陷使用 `fail`；只有 SPEC 无法支持可实现的计划时才使用 `scope_gap`。避免因纯样式问题阻断流程。
5. 使用严格 v3 metadata 完成，必含 `protocol_version=hollysys-controller/v1`、卡片 `iteration` 和完整上下文。pass 必须包含非空路径、digest、review commit；scope_gap 必须给目标和问题。必须通过仓库 Schema，不得包含下一卡字段。绝不得编辑 PLAN、创建 TASKS/卡片、push、创建 MR 或合并。
