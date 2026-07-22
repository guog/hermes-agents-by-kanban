# 飞书命令与通知模板

## 人类命令

```text
实现 PRD <GitLab PRD MR URL>
状态 <run_key>
暂停 <run_key>
继续 <run_key>
重试 <kanban-task-id>
取消 <run_key>
```

## 已接受

```text
已接受 PRD 自动交付。
run=<run_key> project=<group/project> prd=<url>@<short-sha>
当前阶段=run-init；状态与恢复以 Hermes Kanban 为准。
```

## 需要人类

```text
自动交付已暂停，需要人类处理。
run=<run_key> stage=<stage> task=<task-id>
原因=<concise reason>
需要=<specific action>
GitLab/Kanban=<url or command hint>
```

## SPEC 已交付

```text
SPEC 已完成并合入代码。
run=<run_key> spec=<spec-key> code_mr=<url> merge=<short-sha>
下一步=<next spec or finalization>
```

## 全部完成

```text
PRD 自动交付完成。
run=<run_key> project=<group/project>
SPEC=<count> merged_code_mrs=<urls>
验证摘要=<short summary>
残余风险=<none or links>
```

