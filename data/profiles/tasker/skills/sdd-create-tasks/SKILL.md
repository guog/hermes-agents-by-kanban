---
name: sdd-create-tasks
description: 在共享交付分支上编写完整且一一对应的 TASKS 集合
version: 0.2.1
---

# 创建 TASKS 集合

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 为检查 worktree、commit 和 push 而明确要求的常规 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是 Agent 所属交付分支唯一可编辑的工作副本。dispatcher 完成对账后，不要例行运行 `git fetch origin` 或循环拉取。只有在恢复缺失的 ref/worktree、调查已证实的本地/远端头提交不一致或 push 被拒绝，或满足 `live_reconcile_required` 时才能 fetch，并记录原因。使用本地 `git rev-parse`/`git status` 判断 worktree 状态，使用 `glab` 获取当前 MR 状态。
- PRD 存在遗漏或歧义，本身不等于 `scope_gap` 或 `needs_input`。应依次根据明确的验收条件/约束、当前仓库行为和约定、已批准的上游产物、兼容性/安全性作出判断，并选择最小且可逆的范围。若决策影响用户可见的范围/验收、公共接口、数据/迁移、安全/权限、兼容性、恢复/回滚或必需的测试/门禁，则属于关键决策。完成前，使用 `/opt/fleet/templates/decision-comment.md` 对账一条幂等的交付 MR 评论，其中包含本卡片作出的每项关键决策，并将其 URL 写入完成元数据的 `gitlab_urls`；若没有关键决策，不要发布空评论。非关键假设记录在 TASKS 或完成证据中。只有当证据互相矛盾且不存在能保留验收语义的安全选择，或确实缺少权限、凭据或能力时，才上报。

1. 调用 `kanban_show()`；要求卡片来自 dispatcher，并验证项目、运行、共享 worktree/分支/MR 以及仍然有效的 SPEC 和 PLAN 摘要。
2. 对每个匹配的 SPEC/PLAN 键，基于中文模板 `/opt/fleet/templates/tasks-template.md` 编写且只编写对应的 `docs/prds/<prd-basename>/tasks/task-<key>.md`。替换所有占位符，返工时保留稳定的 ID。
3. 保持严格的 `- [ ] T001 [P?] [US?] ...` 行格式，并在整个 PRD 的完整 TASKS 集合中唯一分配 ID；绝不得在每个文件中从 T001 重新开始。每项任务都要定义 SPEC/需求来源、准确的目标文件、`depends_on`、客观验收条件，以及可重复执行的测试或验证。提供无环的跨文件 DAG、执行波次和完整覆盖矩阵。
4. 只修复任务缺陷。如果 PLAN 或 SPEC 不充分，则以 `scope_gap` 完成；不得暗中改写已批准的需求/设计，也不得重编号无关工作。
5. commit 最小且连贯的 TASKS 变更，并 push 到同一分支/MR。绝不得创建 GitLab Issue、Task work item 或 TASKS MR。
6. 完成时提供排序后的 TASKS 路径/blob SHA、图验证结果、commit/头提交、MR 和剩余风险。传入一个符合 `/opt/fleet/schemas/card-completion.schema.json` 的扁平 v2 元数据对象；在顶层重复卡片中所有必需的 project/workspace/source 字段，允许使用 schema 规定的 null/空数组，但不得省略键。不得实现、审查或合并。
