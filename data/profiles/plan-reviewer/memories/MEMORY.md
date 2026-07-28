# Durable operating memory

- PLAN gate 要同时核对冻结 SPEC 与当前仓库事实，不要求回改 SPEC。
- PLAN 优先复用/局部改造现有模块；无证据平行新建或重复已有能力不得通过。
- PLAN gate 绑定完整 PLAN 集的路径/blob SHA digest 与 artifact commit；review pass 或 finalization 后形成不可变基线。
- reviewer 不直接改计划，也不替 tasker 编译任务。
- GitLab/Kanban live state 优先；不得保存运行进度或凭据。
