# Durable operating memory

- TASKS analysis 是独立只读门禁，不是 tasker 自检。
- 必须验证 DAG、覆盖矩阵、稳定 ID 和跨工件一致性。
- 每个任务必须落到真实仓库路径并声明 reuse/modify/extend/create。
- TASKS gate 绑定完整 TASKS 集的路径/blob SHA digest 与 artifact commit；review pass 或 finalization 后形成不可变基线。
- GitLab/Kanban live state 优先；不得保存运行进度或凭据。
