SDD-GATE: v=1 run=<run_key> stage=<spec-review|plan-review|tasks-analyze|test|code-review> result=<pass|fail|scope_gap> head=<40-char-sha> task=<kanban-task-id>

## 结论

<一句话结论>

## 证据

- 检查对象：<artifact/diff/commit>
- 验证命令：`<command>` → <result>
- 覆盖：<requirements/tasks/tests>

## 发现

| 严重度 | 位置 | 问题 | 必需动作 |
| --- | --- | --- | --- |
| <level> | <file/section> | <finding> | <action> |

## 未覆盖与残余风险

- <risk-or-none>

## 结构化摘要

```json
{
  "schema_version": 1,
  "run_key": "<run_key>",
  "stage": "<stage>",
  "result": "<pass|fail|scope_gap>",
  "head_sha": "<40-char-sha>",
  "kanban_task_id": "<task-id>"
}
```

