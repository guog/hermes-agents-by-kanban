HOLLYSYS-GATE: v=5 run=<run_key> stage=<spec-review|plan-review|tasks-review|test|code-review> result=<pass|fail|cancelled> digest=<sha256-or-na> artifact=<40-char-sha-or-na> head=<40-char-sha-or-na> test=<executed|skipped_unavailable|na> task=<kanban-card-id>
HOLLYSYS-SEMANTIC-GATE: v=1 run=<run_key> phase=<implementation_entry|implementation_completion|migration_execution|deployment_entry|release_acceptance> decision=<approved|rejected> artifact=<frozen-tasks-commit> digest=<frozen-tasks-digest>

## 结论

<一句话结论>

## 检查基线

- 工件门禁：按路径排序，将每行 `<path>\0<git-blob-sha>\n` 拼接后计算 SHA-256；pass/fail 都填写 `artifact_digest` 和审查时的 `artifact_commit_sha`。
- 代码门禁：tester 与 code-reviewer 必须填写同一个当前 MR `head_sha`；工件字段可填写已批准基线。
- tester 正常执行测试时填写 `test=executed`。只有浏览器、硬件或外部环境等必要
  条件经预检确认不可用时才填写 `test=skipped_unavailable`，同时在结构化摘要中
  记录原因、已执行的替代检查和残余风险。其他 gate 填写 `test=na`。
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

## Controller v8 证据

- source_key：`<source_key>`
- run_generation：`<run_generation>`
- context_digest：`<context_digest>`
- validator/version：`<validator>` / `<validator_version>`
- validator input/result digest：`<input_digest>` / `<result_digest>`
- completion v8 digest：`<sha256>`

完整 completion 必须由 `hollysysctl completion-template` 生成并经
`validate-completion` 验证，不在评论中手写或复制全量对象。
