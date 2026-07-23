# Durable operating memory

- PRD 是正式 SDD run 的人工审查入口，不是聊天摘要。
- PRD 必须明确范围、可验证结果和阻塞型待确认项。
- 每个 PRD 使用自己的分支和 PRD MR；只有人类合入后，dispatcher 才能以该版本启动单 PRD 单交付 MR run。
- Kanban/GitLab live state 高于本文件；不得在此保存 token 或运行进度。
