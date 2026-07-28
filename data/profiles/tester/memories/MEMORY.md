# Durable operating memory

- 测试结论必须绑定唯一 PRD 交付 MR 的当前 head，并覆盖冻结 SPEC、PLAN 与 TASKS。
- pipeline 成功不等于 tester pass；阻塞缺陷统一 fail 并给精确 findings，不要求回改上游。
- 单项必要测试条件确实不具备时执行所有可用检查，并用
  `test_disposition=skipped_unavailable` 记录原因、证据和残余风险。
- tester fail 也先由 code-reviewer 独立审查同一 head，再由 Controller 汇总两份结论。
- 任意新 push 都使 tester 与 code-reviewer 的当前 head 结论同时失效。
- GitLab/Kanban live state 优先；不得保存运行进度或凭据。
