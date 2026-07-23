# Durable operating memory

- PLAN gate 要同时核对 approved SPEC 与当前仓库事实。
- PLAN gate 绑定完整 PLAN 集的路径/blob SHA digest 与 review commit；被审查 PLAN 变化会使本阶段及下游失效。
- reviewer 不直接改计划，也不替 tasker 编译任务。
- GitLab/Kanban live state 优先；不得保存运行进度或凭据。
