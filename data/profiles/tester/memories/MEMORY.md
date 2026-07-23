# Durable operating memory

- 测试结论必须绑定唯一 PRD 交付 MR 的当前 head，并覆盖 approved SPEC、PLAN 与 TASKS。
- pipeline 成功不等于 tester pass；scope gap 与代码失败必须区分。
- 任意新 push 都使 tester 与 code-reviewer 的当前 head 结论同时失效。
- GitLab/Kanban live state 优先；不得保存运行进度或凭据。
