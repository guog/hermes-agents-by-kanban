# Durable operating memory

- code-review pass 与 tester pass 必须针对唯一 PRD 交付 MR 的同一当前 head。
- tester fail 或结构化跳过不短路代码审查；Controller 汇总两份独立结论。
- 内网且设计并发不超过 1000 人，不用泛化的互联网级安全/并发假设制造阻塞；
  看板、工业流程图/P&ID、实时刷新和重渲染路径仍重点检查性能。
- review 结论来自 diff、冻结工件和证据，不来自作者声明。
- 任意新 push 都使两个 head gate 同时失效，必须重新测试和审查。
- GitLab/Kanban live state 优先；不得保存运行进度或凭据。
