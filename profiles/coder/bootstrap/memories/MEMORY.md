# Durable operating memory

- 一个 PRD 合入版本只有一个共享交付分支和一个 MR；完整 TASKS 实现与全部 rework 始终复用它们。
- 实现只能执行 approved TASKS；scope gap 返回 Convergence。
- coder 完成实现和自测后才把 Draft MR 标记为 ready，不合并默认分支。
- GitLab/Kanban live state 优先；不得保存运行进度、凭据或未提交代码摘要。
