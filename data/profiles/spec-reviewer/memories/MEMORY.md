# Durable operating memory

- SPEC gate 绑定排序后的 SPEC 路径/blob SHA digest 与 review commit，并可追溯到已合入 PRD。
- 后续新增 PLAN、TASKS 或代码不使 SPEC gate 失效；已通过的 SPEC 被修改时，该门禁及下游门禁失效。
- reviewer 提问题和结论，不直接修产物。
- GitLab/Kanban live state 优先；不得保存运行进度或凭据。
