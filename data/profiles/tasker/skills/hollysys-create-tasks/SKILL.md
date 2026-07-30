---
name: hollysys-create-tasks
description: 在 v4 唯一 Delivery 上生成经共享确定性 validator 验证的 TASKS DAG。
version: 4.0.0
---

# 创建 TASKS

- 只操作 Controller 指定 worktree、branch 和绑定 MR；不得创建/选择分支、MR、Issue
  或 GitLab Task。GitLab 操作只用锁定的 `glab`。

1. 通过 `hollysysctl card-context --card-id "$HERMES_KANBAN_TASK"` 获取受信 v4 上下文，
   保存到其 `scratch_dir`，核对 head/context/Delivery/frozen baseline。
2. 在 `repository_base_sha` 核实 PLAN 引用的真实代码和测试。为每个 SPEC/PLAN 键
   只写对应 `task-<key>.md`，保持全局唯一稳定 ID、显式依赖、无环 DAG、执行波次、
   覆盖、验收与可重复验证。每项声明 `reuse|modify|extend|create`；`create` 必须有
   现有能力无法承载的证据。
3. repair 只处理 TASKS；冻结违规先恢复 baseline。commit/push 当前唯一分支。
4. Writer 与 Reviewer 必须使用同一入口：
   `hollysysctl validate-artifact --card-id "$HERMES_KANBAN_TASK"`。重复 ID、缺依赖、
   自依赖、环、非法动作或冻结文件修改按稳定 error code 修复。不是 `ok=true` 或
   validator 不可用时不得 pass。
5. 用 `completion-template` 生成 completion v8，保留 Controller 生成的 validator
   version、input/result digest 和 result，只补充真实 `repository_evidence` 与验证。通过
   `validate-completion` 后完成卡片并立即结束。
