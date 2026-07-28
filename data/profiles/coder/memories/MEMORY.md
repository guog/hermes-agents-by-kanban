# Durable operating memory

- 一个 PRD 合入版本只有一个共享交付分支和一个 MR；完整 TASKS 实现与全部修复始终复用它们。
- 企业 MES 定制以 repository base 为起点，优先复用、修改或扩展现有能力，不从零重建。
- 实现只执行冻结 TASKS；上游遗漏、歧义或矛盾由 CODE 阶段自主解释，不回改冻结工件。
- CODE 双门禁失败时一次性处理同一 head 的 tester 与 code-reviewer 全部意见；
  `code_modification=n/5` 由 Controller 计数，最多修改 5 次。
- coder 完成实现和自测后才把 Draft MR 标记为 ready，不合并默认分支。
- GitLab/Kanban live state 优先；不得保存运行进度、凭据或未提交代码摘要。
