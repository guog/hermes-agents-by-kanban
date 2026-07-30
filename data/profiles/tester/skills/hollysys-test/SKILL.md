---
name: hollysys-test
description: 独立测试 v4 绑定 Delivery 的准确当前 head。
version: 4.0.0
---

# 测试交付 head

- 只读 Controller 指定 worktree、branch、绑定 MR/head 与冻结基线；不得改代码、
  push、创建/选择 MR。GitLab 操作只用锁定的 `glab`。

1. 通过 `hollysysctl card-context --card-id "$HERMES_KANBAN_TASK"` 获取受信 v4 上下文，
   保存到其 `scratch_dir`。测试前核对绑定 MR 当前 head
   等于 expected head。
2. 建立 SPEC/PLAN/TASKS 到测试的覆盖清单，运行离线条件下可执行的静态、单元、
   集成/契约检查及流水线核对。先真实预检，不能因耗时或可能失败而跳过。
3. 关键 validator 缺失时返回 `tool_unavailable`，不得声称通过。确实不可用的外部
   测试可用 `skipped_unavailable`，但必须有具体预检、替代检查和非空 residual risk。
4. 发布前重读 MR head；变化则以 `cancelled` 完成本次陈旧 attempt。未变化时发布
   绑定 card/head 的幂等 test gate；阻塞实现缺陷用 `fail`。
5. 用 `completion-template` 生成 pass/fail/cancelled completion v8，只填写测试、
   gate、issues 和风险，不加入 authoring-only repository evidence。通过
   `validate-completion` 后完成卡片并立即结束。
