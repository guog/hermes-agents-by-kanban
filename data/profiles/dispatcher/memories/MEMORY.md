# Durable operating memory

- 正式运行始于人类同时指定精确 PRD blob/raw URL 与已合入的 PRD MR。
- 一个 PRD 合入版本只使用一个 run、一个共享分支和一个 Draft MR，SPEC、PLAN、TASKS 均整批过门禁。
- 设计返工最多 3 轮，代码返工最多 5 轮；超限进入 HUMAN-DECISION。
- tester 与 code-reviewer 的 pass 必须绑定同一个当前 head SHA；head 变化即失效。
- Kanban/GitLab live state 高于本文件；本文件不得保存运行进度、token 或聊天正文。
