---
name: sdd-review-plan
description: 审查完整的 PLAN 集合，并在共享 MR 上发布绑定摘要的门禁
version: 0.3.0
---

# 审查 PLAN 集合

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 明确要求的常规只读 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是 Agent 所属交付分支的工作副本。dispatcher 完成对账后，不要例行运行 `git fetch origin` 或循环拉取。只有在恢复缺失的 ref/worktree、调查已证实的本地/远端头提交不一致，或满足 `live_reconcile_required` 时才能 fetch，并记录原因。使用本地 `git rev-parse`/`git status` 判断产物状态，使用 `glab` 获取当前 MR 状态。
- PRD 存在遗漏或歧义，本身不构成阻断性问题。应依次根据明确的验收条件/约束、当前仓库行为和约定、已批准的上游产物、兼容性/安全性作出判断，并选择最小且可逆的范围。若决策影响用户可见的范围/验收、公共接口、数据/迁移、安全/权限、兼容性、恢复/回滚或必需的测试/门禁，则属于关键决策。将每项关键决策写入幂等门禁 MR 评论的 `## 关键自主决策` 章节，并把该评论 URL 写入完成元数据的 `gitlab_urls`；如果没有关键决策，在已有门禁评论中填写 `无`，不要另外发布空评论。只有当证据互相矛盾且不存在能保留验收语义的安全选择，或确实缺少权限、凭据或能力时，才上报。
- 真正需要人类时，严格执行卡片中的“人类阻塞协议”：确认自己的原渠道订阅仍存在，写入幂等 `[human-block:v1]` 评论，再 `kanban_block`；不得自行发飞书、退订、unblock 或创建恢复卡。正常完成前退订当前卡。

1. 调用 `kanban_show()`；要求卡片来自 dispatcher，并验证项目、运行、worktree、分支、共享 MR 以及仍然有效的 SPEC 摘要。
2. 为每个 SPEC 键枚举一个 `plan-<key>.md`。读取 `/opt/fleet/templates/plan-template.md`；检查其中实质性的必需章节、SPEC 可追溯性、相对于实际代码的可行性、架构适配、接口、数据/迁移、兼容性、安全性、可观测性、测试、回滚、未解决决策和过度设计。报告实质性遗漏，不报告纯样式差异。
3. 在审查 commit 上计算排序后的 path/blob-SHA 摘要。发布一条包含摘要和 `review_commit_sha` 的幂等 v2 `plan-review` 门禁。
4. PLAN 缺陷使用 `fail`；只有 SPEC 无法支持可实现的计划时才使用 `scope_gap`。避免因纯样式问题阻断流程。
5. 使用 pass/fail/scope_gap 元数据和证据完成卡片。传入一个符合 `/opt/fleet/schemas/card-completion.schema.json` 的扁平 v2 元数据对象；在顶层重复卡片中所有必需的 project/workspace/source 字段。通过结论必须包含非空的 `artifact_paths`、`artifact_digest` 和 `review_commit_sha`。绝不得编辑 PLAN、创建 TASKS、push、创建另一个 MR 或合并。
