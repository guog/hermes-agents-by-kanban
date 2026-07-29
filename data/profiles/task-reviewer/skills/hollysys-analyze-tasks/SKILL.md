---
name: hollysys-analyze-tasks
description: 当 Kanban 卡要求审查 TASKS 时，核对 SPEC/PLAN 映射、DAG、覆盖与验收并发布门禁。
version: 2.0.1
---

# 审查 TASKS 集合

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 明确要求的常规只读 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是准确工作副本。Controller 已在发卡前对账；不要例行 fetch/pull。只在缺少 ref 或已证实头不一致时 fetch，并记录原因。
- 上游遗漏、歧义或矛盾不得要求回改冻结 SPEC/PLAN。检查 TASKS 的当前阶段决策是否安全、完整且可执行；只有 coder 无法安全开工的任务缺陷才 fail，其余记为 residual risk。
- 只有权限、凭据、环境/能力缺失、自动重试不安全或破坏性动作待授权时才允许人类阻塞。

1. 调用 `kanban_show()`；要求 `created_by=hollysys-controller`，并验证卡片 JSON 的 v3 protocol/mode/run/stage/iteration/assignee/parent、项目、worktree、分支、共享 MR 和冻结 PRD/SPEC/PLAN 基线。
2. 读取 TASKS 模板并在 `repository_base_sha` 上核实目标路径和已有能力；验证
   SPEC/PLAN/TASK 键、稳定唯一 ID、显式依赖、DAG、执行波次、需求覆盖、验收和测试。
   每项任务必须有 `reuse|modify|extend|create` 动作，新增结构必须有“现有能力无法
   承载”的仓库证据。绿地式搭架子、重复造已有能力或目标路径臆造均属实质性缺陷。
3. 在审查 commit 上计算完整 TASKS path/blob 摘要，发布包含路径、digest、`artifact_commit_sha`、card ID 和评论 URL 的幂等 v5 gate。
4. 阻塞性任务缺陷使用 `fail` 并给 tasker 可在 TASKS 内完成的动作；不得跨阶段回退。
5. 使用 v3 normal metadata；pass/fail 都绑定 paths/digest/artifact commit 和 gate URL，pass 填 `baseline_disposition=reviewed`，fail 给非空 issues，并把门禁评论中的关键自主决策摘要写入 `key_decisions`。不得编辑产物、实现、push、创建卡片或合并。
   审查必须核实仓库证据，但 completion metadata 不得包含仅 authoring pass 可用的
   `repository_evidence`；仓库核查结果写入 gate 评论、`verification` 和
   `key_decisions`。
- 调用完成工具前，必须将业务 metadata 保存为 JSON，并执行
  `hollysysctl validate-completion --card-id '<当前卡 ID>' --metadata '<json-file>'`；
  只有 Controller 返回 `ok=true` 才能完成卡片。
