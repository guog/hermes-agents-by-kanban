---
name: sdd-analyze-tasks
description: 审查完整的 SPEC、PLAN、TASKS 映射并发布绑定摘要的门禁
version: 0.2.1
---

# 审查 TASKS 集合

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 明确要求的常规只读 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是 Agent 所属交付分支的工作副本。dispatcher 完成对账后，不要例行运行 `git fetch origin` 或循环拉取。只有在恢复缺失的 ref/worktree、调查已证实的本地/远端头提交不一致，或满足 `live_reconcile_required` 时才能 fetch，并记录原因。使用本地 `git rev-parse`/`git status` 判断产物状态，使用 `glab` 获取当前 MR 状态。
- PRD 存在遗漏或歧义，本身不构成阻断性问题。应依次根据明确的验收条件/约束、当前仓库行为和约定、已批准的上游产物、兼容性/安全性作出判断，并选择最小且可逆的范围。若决策影响用户可见的范围/验收、公共接口、数据/迁移、安全/权限、兼容性、恢复/回滚或必需的测试/门禁，则属于关键决策。将每项关键决策写入幂等门禁 MR 评论的 `## 关键自主决策` 章节，并把该评论 URL 写入完成元数据的 `gitlab_urls`；如果没有关键决策，在已有门禁评论中填写 `无`，不要另外发布空评论。只有当证据互相矛盾且不存在能保留验收语义的安全选择，或确实缺少权限、凭据或能力时，才上报。

1. 调用 `kanban_show()`；要求卡片来自 dispatcher，并验证项目、运行、worktree、分支、共享 MR 以及仍然有效的 SPEC/PLAN 摘要。
2. 读取 `/opt/fleet/templates/tasks-template.md`；验证一一对应的 SPEC/PLAN/TASK 键集合、严格的检查清单行、稳定且唯一的 ID、显式 `depends_on`、无环依赖、执行波次、需求覆盖、准确的目标路径、客观验收条件，以及足够且可重复执行的测试工作。报告实质性执行缺陷，不报告纯样式差异。
3. 在审查 commit 上计算完整且排序后的 TASKS path/blob-SHA 摘要。发布一条包含摘要和 `review_commit_sha` 的幂等 v2 `tasks-review` 门禁。
4. 任务缺失或错误时使用 `fail`。只有存在证据表明 PLAN 或 SPEC 有缺陷时才使用 `scope_gap`，并指出负责的上游阶段。
5. 完成时提供 pass/fail/scope_gap、摘要、审查 commit 和问题。传入一个符合 `/opt/fleet/schemas/card-completion.schema.json` 的扁平 v2 元数据对象；在顶层重复卡片中所有必需的 project/workspace/source 字段。通过结论必须包含非空的 `artifact_paths`、`artifact_digest` 和 `review_commit_sha`。绝不得编辑产物、实现、push、创建另一个 MR 或合并。
