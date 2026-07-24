---
name: sdd-implement
description: 在唯一的共享 PRD 交付 MR 上实现或返工所有已批准的 TASKS
version: 0.2.0
---

# 实现 PRD

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 为检查 worktree、commit 和 push 而明确要求的常规 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是 Agent 所属交付分支唯一可编辑的工作副本。dispatcher 完成对账后，不要例行运行 `git fetch origin` 或循环拉取。只有在恢复缺失的 ref/worktree、调查已证实的本地/远端头提交不一致或 push 被拒绝，或满足 `live_reconcile_required` 时才能 fetch，并记录原因。使用本地 `git rev-parse`/`git status` 判断 worktree 状态，使用 `glab` 获取当前 MR 状态。
- PRD 存在遗漏或歧义，本身不等于 `scope_gap` 或 `needs_input`。应依次根据明确的验收条件/约束、当前仓库行为和约定、已批准的上游产物、兼容性/安全性作出判断，并选择最小且可逆的范围。若决策影响用户可见的范围/验收、公共接口、数据/迁移、安全/权限、兼容性、恢复/回滚或必需的测试/门禁，则属于关键决策。完成前，使用 `/opt/fleet/templates/decision-comment.md` 对账一条幂等的交付 MR 评论，其中包含本卡片作出的每项关键决策，并将其 URL 写入完成元数据的 `gitlab_urls`；若没有关键决策，不要发布空评论。非关键假设只在确有需要时写入代码注释，或记入完成证据。只有当证据互相矛盾且不存在能保留验收语义的安全选择，或确实缺少权限、凭据或能力时，才上报。

1. 调用 `kanban_show()`；要求卡片来自 dispatcher，并验证准确的项目、共享 worktree/分支、PRD、交付 MR 以及已批准的 SPEC/PLAN/TASKS 摘要。绝不得创建另一个 checkout、分支或 MR。
2. 写入前，对账当前分支、commit、MR 评论、流水线和已有的部分实现。按依赖顺序执行完整的任务 DAG，并保留仓库中无关的改动。
3. 添加或更新必需的测试。遵循仓库约定，以最小且连贯的实现或返工单元执行 commit/push。
4. 运行与改动相称的格式化、静态检查、单元测试、集成/契约检查，并在同一个 MR 描述中记录准确的命令和结果。
5. 按上述决策层级解决一般性遗漏和歧义。如果已批准产物互相矛盾，或无法支持任何能保留验收语义的安全实现，则停止，并用 `scope_gap` 提供证据及负责的上游阶段；绝不得改变明确的需求意图。
6. 覆盖所有 TASK 且自测通过后，更新 `/opt/fleet/templates/mr-description.md` 所定义的元数据，并将现有 Draft MR 标记为 ready。代码返工时保持 ready，除非 GitLab 策略要求变更期间设为 draft。
7. 完成时提供任务覆盖、变更文件、命令、MR/当前头提交和剩余风险。传入一个符合 `/opt/fleet/schemas/card-completion.schema.json` 的扁平 v2 元数据对象；在顶层重复卡片中所有必需的 project/workspace/source 字段，允许使用 schema 规定的 null/空数组，但不得省略键。不得自我审查或合并。
