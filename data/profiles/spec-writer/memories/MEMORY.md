# Durable operating memory

- SPEC 只定义需求意图与可验证结果，不包含实现决策。
- 一个 PRD 的完整 SPEC 集写入同一共享分支，并在首个有效 SPEC commit 后创建唯一 Draft MR。
- SPEC 集整批审查通过后才进入 PLAN，不按 SPEC 拆分分支或 MR。
- GitLab PRD commit 与 Kanban handoff 是输入真相；不保存运行进度或凭据。
