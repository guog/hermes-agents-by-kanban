# Durable operating memory

- SPEC gate 绑定排序后的 SPEC 路径/blob SHA digest 与 artifact commit，并可追溯到固定 PRD blob。
- SPEC 必须以 repository base 的现有 MES 行为为起点，说明现状与 PRD 增量。
- review pass 或第三次失败后的 finalization 形成不可变 SPEC 基线；后续阶段不得修改。
- reviewer 提问题和结论，不直接修产物。
- GitLab/Kanban live state 优先；不得保存运行进度或凭据。
