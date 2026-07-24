---
name: hollysys-write-spec
description: 当 Kanban 卡要求编写或返工 SPEC 时，在唯一共享 MR 生成完整可测试的 SPEC 集。
version: 0.4.0
---

# 编写 SPEC 集合

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 为检查 worktree、commit 和 push 而明确要求的常规 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是 Agent 所属交付分支唯一可编辑的工作副本。dispatcher 完成对账后，不要例行运行 `git fetch origin` 或循环拉取。只有在恢复缺失的 ref/worktree、调查已证实的本地/远端头提交不一致或 push 被拒绝，或满足 `live_reconcile_required` 时才能 fetch，并记录原因。使用本地 `git rev-parse`/`git status` 判断 worktree 状态，使用 `glab` 获取当前 MR 状态。
- PRD 存在遗漏或歧义，本身不等于 `needs_input`。应依次根据明确的验收条件/约束、当前仓库行为和约定、已批准的上游产物、兼容性/安全性作出判断，并选择最小且可逆的范围。若决策影响用户可见的范围/验收、公共接口、数据/迁移、安全/权限、兼容性、恢复/回滚或必需的测试/门禁，则属于关键决策。首次创建 MR 前，将交接过来的所有 MR 创建前决策和每项关键 SPEC 决策直接写入 `/opt/fleet/templates/mr-description.md` 的 `## 关键自主决策` 章节；SPEC 返工时更新同一章节，不要发布初始决策评论。非关键假设记录在 SPEC 或完成证据中。只有当证据互相矛盾且不存在能保留验收语义的安全选择，或确实缺少权限、凭据或能力时，才上报。
- 真正需要人类时，严格执行卡片中的“人类阻塞协议”：确认自己的原渠道订阅仍存在，写入幂等 `[human-block:v1]` 评论，再 `kanban_block`；不得自行发飞书、退订、unblock 或创建恢复卡。正常完成前退订当前卡。

1. 调用 `kanban_show()` 并要求 `created_by=dispatcher`；对照 GitLab 验证项目 ID/path、共享 worktree/分支、准确的 PRD path/commit/MR 以及运行键。绝不得发现或克隆另一个仓库。
2. 读取准确 commit 上的 PRD。只按可以独立理解和测试的业务范围拆分；定义有序的键、依赖关系和完整的 PRD 覆盖矩阵。
3. 基于中文模板 `/opt/fleet/templates/spec-template.md` 编写每个 `docs/prds/<prd-basename>/specs/spec-<key>.md`。键必须是稳定的小写 kebab-case。替换所有占位符，保留所有必需章节和 PRD 覆盖矩阵，并排除实现选择。
4. 返工时，只修改有证据支持的 SPEC 文件；除非问题要求变更集合，否则保留原有键。不得编写 PLAN/TASKS/代码。
5. 按仓库的约定式提交规则 commit 最小且连贯的 SPEC 变更，并 push 到共享分支。首次有效 SPEC commit 后，先对账，再填充 MR 描述（包括 `## 关键自主决策`；没有时填写 `无`），然后使用 `/opt/fleet/templates/mr-description.md` 创建且只创建一个 `Draft: [PRD] <prd-basename>.md` MR；此后只更新该 MR 及其决策章节。
6. 完成时提供排序后的 SPEC 路径/blob SHA、commit/头提交、MR IID/URL、覆盖情况和剩余风险。传入一个符合 `/opt/fleet/schemas/card-completion.schema.json` 的扁平 v2 元数据对象；在顶层重复卡片中所有必需的 project/workspace/source 字段，允许使用 schema 规定的 null/空数组，但不得省略键。将 MR URL 写入 `gitlab_urls`，作为关键决策的审计位置。不得审查、标记 ready 或合并。
