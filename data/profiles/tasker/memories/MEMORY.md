# Durable operating memory

- TASKS 是 SPEC 到 IMPLEMENT 的稳定执行合同，不是 Kanban 运行状态镜像。
- Task ID、依赖 DAG、覆盖矩阵和规范化摘要必须稳定。
- 每个 PLAN 对应一个同 key TASKS 文件，完整 TASKS 集沿用当前 PRD 的共享分支和唯一 Draft MR。
- GitLab/Kanban live state 优先；不得保存运行进度或凭据。
