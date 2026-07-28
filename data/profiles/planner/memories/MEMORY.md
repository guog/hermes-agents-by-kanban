# Durable operating memory

- PLAN 定义 HOW 与验证策略，不生成最终稳定 Task ID。
- 设计必须以冻结 SPEC 和现有项目约束为输入；上游问题由 PLAN 阶段自主解释，不回改 SPEC。
- 每个 SPEC 对应一个同 key PLAN，完整 PLAN 集沿用当前 PRD 的共享分支和唯一 Draft MR。
- GitLab/Kanban live state 优先；不得保存运行进度或凭据。
