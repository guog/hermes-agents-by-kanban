---
name: hollysys-review-plan
description: 在 v4 受信上下文中确定性校验并审查 PLAN。
version: 4.0.0
---

# 审查 PLAN

- 只读 Controller 指定 worktree、branch、绑定 MR/head 和冻结基线；不得编辑、push、
  创建/选择 MR。GitLab 操作只用锁定的 `glab`。

1. 通过 `hollysysctl card-context --card-id "$HERMES_KANBAN_TASK"` 获取受信 v4 上下文，
   保存到其 `scratch_dir`。
2. 执行 `hollysysctl validate-artifact --card-id "$HERMES_KANBAN_TASK"`，核对 validator
   version、input/result digest 与 error codes；确定性失败或工具不可用时不得 pass。
   必须以 `repository_base_sha` 拒绝绿地式平行框架或重复实现已有能力。
3. 核对每个 SPEC 的 PLAN、真实仓库路径、复用/改造决策、架构、接口、数据迁移、
   兼容性、可观测性、测试和回滚。只有 TASKS 无法安全开展的缺陷才 fail，其余记为
   residual risk。
4. 发布绑定 card/head 和完整 artifact evidence 的幂等 gate 评论。使用
   `completion-template` 生成 pass/fail completion v8，只填写审查证据，不加入
   authoring-only repository evidence。通过 `validate-completion` 后完成卡片并立即
   结束。
