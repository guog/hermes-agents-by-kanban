# Durable operating memory

- 正式运行始于人类同时指定精确 PRD blob/raw URL 与已合入的 PRD MR。
- 一个 PRD 合入版本只使用一个 run、一个共享分支和一个 Draft MR；PRD 启动时固定 blob，SPEC、PLAN、TASKS 各自形成不可变基线。
- run 固定 repository base；SPEC、PLAN、TASKS、CODE 都是对现有企业 MES 的复用、
  扩展或修改，不是绿地项目。
- 每个文档阶段最多三次 review；第三次失败后只做一次 finalization 并强制收敛继续。
- tester 与 code-reviewer 必须检查同一个当前 head；任一失败时汇总两者意见让 coder
  修改，最多 5 次。第 5 次修改后的版本仍未双通过则结束自动流程并通知人类。
- 必要测试条件不具备时 tester 可结构化跳过测试，并报告残余风险。
- Dispatcher 是人类命令、状态和异常入口；Controller 独立持续推进并负责正式卡、门禁和 checked-head merge。
- Kanban/GitLab live state 高于本文件；本文件不得保存运行进度、token 或聊天正文。
