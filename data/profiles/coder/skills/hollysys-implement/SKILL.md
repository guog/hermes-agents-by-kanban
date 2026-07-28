---
name: hollysys-implement
description: 当 Kanban 卡要求实现或返工时，在唯一共享 MR 完成已批准 TASKS 及测试。
version: 1.0.0
---

# 实现 PRD

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 为检查 worktree、commit 和 push 而明确要求的常规 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是唯一可编辑副本。Controller 已在发卡前对账；不要例行 fetch/pull。只有缺少 ref、已证实头不一致或 push 被拒绝时才能 fetch，并记录原因。
- PRD 存在遗漏或歧义，本身不等于 `scope_gap` 或 `needs_input`。应依次根据明确的验收条件/约束、当前仓库行为和约定、已批准的上游产物、兼容性/安全性作出判断，并选择最小且可逆的范围。若决策影响用户可见的范围/验收、公共接口、数据/迁移、安全/权限、兼容性、恢复/回滚或必需的测试/门禁，则属于关键决策。完成前，使用 `/opt/fleet/templates/decision-comment.md` 对账一条幂等的交付 MR 评论，其中包含本卡片作出的每项关键决策，并将其 URL 写入完成元数据的 `gitlab_urls`；若没有关键决策，不要发布空评论。非关键假设只在确有需要时写入代码注释，或记入完成证据。只有当证据互相矛盾且不存在能保留验收语义的安全选择，或确实缺少权限、凭据或能力时，才上报。
- 真正需要人类时，严格执行卡片中的“人类阻塞协议”：确认自己的原渠道订阅仍存在，写入幂等 `[human-block:v1]` 评论，再 `kanban_block`；不得自行发飞书、退订、unblock 或创建恢复卡。正常完成前退订当前卡。

1. 调用 `kanban_show()`；要求 `created_by=hollysys-controller`，并验证卡片 JSON 的 protocol/run/stage/iteration/assignee/parent、项目、worktree/分支、PRD、交付 MR 和已批准产物。绝不得创建另一个 checkout、分支或 MR。
2. 写入前，对账当前分支、commit、MR 评论、流水线和已有的部分实现。按依赖顺序执行完整的任务 DAG，并保留仓库中无关的改动。
3. 添加或更新必需的测试。遵循仓库约定，以最小且连贯的实现或返工单元执行 commit/push。
4. 运行与改动相称的格式化、静态检查、单元测试、集成/契约检查，并在同一个 MR 描述中记录准确的命令和结果。
5. 按上述决策层级解决一般性遗漏和歧义。如果已批准产物互相矛盾，或无法支持任何能保留验收语义的安全实现，则停止，并用 `scope_gap` 提供证据及负责的上游阶段；绝不得改变明确的需求意图。
6. 覆盖所有 TASK 且自测通过后，更新 `/opt/fleet/templates/mr-description.md` 所定义的元数据，并将现有 Draft MR 标记为 ready。代码返工时保持 ready，除非 GitLab 策略要求变更期间设为 draft。
7. 完成时提交严格 v3 metadata：必含 `protocol_version=hollysys-controller/v1`、卡片 `iteration`、完整上下文、MR/head 和验证；scope_gap 必须给目标与问题。必须通过仓库 Schema，不得包含 continuation/下一卡/merge 字段。不得自审、创建卡片或合并。
