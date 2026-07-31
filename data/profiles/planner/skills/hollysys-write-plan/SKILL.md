---
name: hollysys-write-plan
description: 在 v4 唯一 Delivery 上依据冻结 SPEC 编写并验证 PLAN。
version: 4.0.0
---

# 编写 PLAN

- 只操作 Controller 指定 worktree、branch 和已绑定 MR；不得 clone、创建/选择分支
  或 MR。GitLab 操作只用锁定的 `glab`。
- PLAN 必须落到 `repository_base_sha` 的真实模块、接口、数据、配置和测试上，优先
  复用或局部改造现有 MES，不创建平行架构。
- 本任务由父 Agent 完成，不调用 `delegate_task`。先限定与 SPEC 直接相关的真实目录，
  再做精确搜索；单轮最多读取 20 个目标文件，不做全仓库扫描或重复审查。

1. 使用 `hollysysctl card-context --card-id "$HERMES_KANBAN_TASK"` 获取并保存受信 v4
   上下文；验证 expected head、context digest、Delivery 和 frozen baseline。
2. 读取仓库规则、相关实现、迁移和测试，为每个 SPEC 键只创建对应
   `plan-<key>.md`。记录复用/修改/扩展点、接口、数据迁移、兼容性、可观测性、
   测试、回滚、追溯与风险，不定义最终 Task ID。
3. repair 只修改受影响 PLAN；冻结违规按 baseline 恢复。若 redispatch 时 artifact
   已 commit/push、worktree clean 且远端 MR head 等于本地 HEAD，直接进入协议收尾，
   不重复修改 PLAN 或扩大搜索。commit/push 当前唯一分支并核对本地 HEAD 等于远端
   branch/MR head，不创建 PLAN MR。
4. 执行 `hollysysctl validate-artifact --card-id "$HERMES_KANBAN_TASK"`；不是
   `ok=true` 时不得 pass。
5. 一旦 artifact 结论稳定，停止搜索并预留预算完成协议。用 `completion-template`
   生成当前 outcome 的 completion v8 文件，不读取完整 schema，只补充真实
   `repository_evidence`、
   验证、决策和风险，不改受信上下文与确定性 checks。`validate-completion` 返回
   `ok=true` 后直接调用 `kanban_complete`，不得用 `execute_code` 包裹这些命令；
   成功后立即结束。
