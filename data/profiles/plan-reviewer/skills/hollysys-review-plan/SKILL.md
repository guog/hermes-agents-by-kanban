---
name: hollysys-review-plan
description: 当 Kanban 卡要求审查 PLAN 时，核对 SPEC、仓库约束与可执行性并发布门禁。
version: 2.0.1
---

# 审查 PLAN 集合

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 明确要求的常规只读 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是准确工作副本。Controller 已在发卡前对账；不要例行 fetch/pull。只在缺少 ref 或已证实头不一致时 fetch，并记录原因。
- 上游遗漏、歧义或矛盾不得要求回改冻结 SPEC。检查 PLAN 的当前阶段自主决策是否安全、自洽、可逆且可实施；只有 TASKS 无法安全开展的缺陷才 fail，其余作为 residual risk 后 pass。
- 只有权限、凭据、环境/能力缺失、自动重试不安全或破坏性动作待授权时才允许人类阻塞。

1. 调用 `kanban_show()`；要求 `created_by=hollysys-controller`，并验证卡片 JSON 的 v3 protocol/mode/run/stage/iteration/assignee/parent、项目、worktree、分支、共享 MR 和冻结 PRD/SPEC 基线。
2. 为每个 SPEC 键枚举一个 `plan-<key>.md`，并在 `repository_base_sha` 上核实其
   现有系统盘点和引用路径。检查 SPEC 可追溯性、相对于实际代码的可行性、复用/改造
   决策、架构适配、接口、数据/迁移、兼容性、可观测性、测试和回滚。默认新建
   平行框架、忽略已有模块/约定、重复实现现有能力或无法落到真实路径均属实质性缺陷。
3. 在审查 commit 上计算 path/blob 摘要，发布包含完整路径、digest、`artifact_commit_sha`、card ID 和评论 URL 的幂等 v5 `plan-review` 评论。
4. PLAN 阻塞缺陷使用 `fail` 并给 planner 可在 PLAN 内执行的动作；不得使用跨阶段结果，纯样式和可延后改进写 residual risk。
5. 使用 v3 normal metadata；pass/fail 都绑定路径、digest、artifact commit 和 gate URL，pass 填 `baseline_disposition=reviewed`，fail 给非空 issues，并把门禁评论中的关键自主决策摘要写入 `key_decisions`。不得编辑 PLAN、创建 TASKS/卡片、push 或合并。
   审查必须核实仓库证据，但 completion metadata 不得包含仅 authoring pass 可用的
   `repository_evidence`；仓库核查结果写入 gate 评论、`verification` 和
   `key_decisions`。
- 调用完成工具前，必须将业务 metadata 保存为 JSON，并执行
  `hollysysctl validate-completion --card-id '<当前卡 ID>' --metadata '<json-file>'`；
  只有 Controller 返回 `ok=true` 才能完成卡片。
