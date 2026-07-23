<!-- SDD-DECISION: v=1 run=<run_key> stage=<stage> card=<kanban-card-id> -->

## 关键自主决策

| 决策 ID | PRD 未明确或模糊点 | 自主决策 | 依据 | 影响与可逆方式 |
| --- | --- | --- | --- | --- |
| `<stable-decision-id>` | <ambiguity> | <decision> | <acceptance/repository/upstream/security evidence> | <scope/compatibility/rollback> |

- MR/head：<mr-iid-and-head-sha>
- 未采用方案：<alternatives-and-reason>
- 残余风险：<risk-or-none>

同一 run、stage、card 重试时先按首行稳定 marker 查找并更新原评论，不重复发布。
