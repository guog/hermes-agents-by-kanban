# Durable operating memory

- code-review pass 与 tester pass 必须针对唯一 PRD 交付 MR 的同一当前 head。
- review 结论来自 diff、approved artifacts 和证据，不来自作者声明。
- 任意新 push 都使两个 head gate 同时失效，必须重新测试和审查。
- GitLab/Kanban live state 优先；不得保存运行进度或凭据。
