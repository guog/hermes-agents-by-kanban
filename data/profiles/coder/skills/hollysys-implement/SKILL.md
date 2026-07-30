---
name: hollysys-implement
description: 在 v4 唯一 Delivery 上实现冻结 TASKS；MR ready 状态由 Controller 管理。
version: 4.0.0
---

# 实现 PRD

- 只操作 Controller 指定 worktree、branch 和绑定 MR；不得 clone、创建/选择分支或
  MR，也不得切换 MR 状态。GitLab 操作只用锁定的 `glab`。
- 以 `repository_base_sha` 的现有 MES 为起点，复用/修改/扩展真实业务链路；不得
  重建平行框架。不得修改任何冻结工件。

1. 通过 `hollysysctl card-context --card-id "$HERMES_KANBAN_TASK"` 获取受信 v4 上下文，
   保存到其 `scratch_dir`，核对 context、expected head、Delivery 和 frozen
   baselines。
2. 读取冻结 TASKS、完整 diff、当前流水线与既有实现，按真实仓库完成最小可逆适配。
   code gate repair 必须同时处理同一 checked head 的 tester 与 code-reviewer findings。
3. 添加必要代码和测试，执行离线依赖模式下与改动相称的格式化、静态、单元、
   集成/契约检查；缺关键工具时报告 `tool_unavailable`，不得声称通过。
4. 约定式 commit/push 当前唯一分支，立即核对本地/远端 head。关键自主决策写入幂等
   MR 评论；没有则不发布空评论。Controller 在接受首次 IMPLEMENT completion 后将
   MR 标记 ready，Agent 不得操作 draft/ready。
5. 用 `completion-template` 生成 completion v8，只补充实际
   `repository_evidence`、验证、决策和
   风险，不改 source/run/context/head_before。通过 `validate-completion` 后调用
   `kanban_complete`，成功后立即结束。
