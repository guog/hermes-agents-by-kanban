SDD-GATE: v=2 run=<run_key> stage=<spec-review|plan-review|tasks-review|test|code-review> result=<pass|fail|scope_gap> digest=<sha256-or-na> review=<40-char-sha-or-na> head=<40-char-sha-or-na> task=<kanban-card-id>

## 结论

<一句话结论>

## 检查基线

- 工件门禁：按路径排序，将每行 `<path>\0<git-blob-sha>\n` 拼接后计算 SHA-256；填写 `artifact_digest` 和审查时的 `review_commit_sha`。
- 代码门禁：tester 与 code-reviewer 必须填写同一个当前 MR `head_sha`；工件字段可填写已批准基线。
- 检查对象：<paths/diff/commit>
- 验证命令：`<command>` → <result>
- 覆盖：<requirements/tasks/tests>

## 发现

| 严重度 | 位置 | 问题 | 必需动作 |
| --- | --- | --- | --- |
| <level> | <file/section> | <finding> | <action> |

## 未覆盖与残余风险

- <risk-or-none>

## 关键自主决策

仅在本轮 gate 对 PRD 未明确或模糊点作出关键自主决策时填写；否则写“无”。
关键决策包括影响用户可见范围/验收、公共接口、数据与迁移、安全与权限、
兼容性、恢复/回滚或必需测试门禁的选择。

| 决策 ID | 模糊点 | 自主决策 | 依据 | 影响与可逆方式 |
| --- | --- | --- | --- | --- |
| `<stable-decision-id-or-none>` | <ambiguity-or-none> | <decision-or-none> | <evidence> | <impact/rollback> |

## 结构化摘要

```json
{
  "schema_version": 2,
  "run_key": "<run_key>",
  "stage": "<stage>",
  "result": "<pass|fail|scope_gap>",
  "artifact_paths": ["<sorted-path>"],
  "artifact_digest": "<sha256-or-null>",
  "review_commit_sha": "<40-char-sha-or-null>",
  "head_sha": "<40-char-sha-or-null>",
  "kanban_card_id": "<card-id>"
}
```
