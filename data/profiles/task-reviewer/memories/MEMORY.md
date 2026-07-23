# Durable operating memory

- TASKS analysis 是独立只读门禁，不是 tasker 自检。
- 必须验证 DAG、覆盖矩阵、稳定 ID 和跨工件一致性。
- TASKS gate 绑定完整 TASKS 集的路径/blob SHA digest 与 review commit；工件变化会使本阶段及下游失效。
- GitLab/Kanban live state 优先；不得保存运行进度或凭据。
