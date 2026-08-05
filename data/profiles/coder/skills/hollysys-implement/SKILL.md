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
4. 严格按冻结 TASKS 的 `execution_wave` 分波实施。每波最多并行委派 3 个路径不重叠
   的 child；child 仅在分配范围内研究或修改文件，不得修改父卡片，不得 commit 或 push。
   父 Agent 汇总后完成定向验证，并为该波创建本地 Conventional Commit。中间波次
   不 push；全部波次通过完整门禁后只 push 一次并核对本地/远端 head。
5. 每波在 Controller 指定的 run scratch 中写入 manifest，至少记录 wave/task、child
   摘要 SHA-256、commit/tree SHA、验证命令及结果；manifest 和完整 child 证据权限
   必须为 `0600`。关键自主决策写入幂等 MR 评论；没有则不发布空评论。Controller
   在接受首次 IMPLEMENT completion 后将 MR 标记 ready，Agent 不得操作 draft/ready。
6. 用 `completion-template` 生成 completion v8，只补充实际
   `repository_evidence`、验证、决策和
   风险，不改 source/run/context/head_before。通过 `validate-completion` 后调用
   `kanban_complete`，成功后立即结束。
