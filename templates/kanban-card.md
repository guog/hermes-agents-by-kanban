# [<run_key>] <STAGE> iteration <n>

正式工作卡由确定性 Controller 生成；本模板说明卡片 JSON 与执行合同。Worker
不得创建/link/promote/unblock 其他正式卡，也不得猜测下一阶段。

```json
{
  "protocol_version": "hollysys-controller/v1",
  "kind": "work",
  "run": {
    "protocol_version": "hollysys-controller/v1",
    "kind": "run-init",
    "run_key": "<hollysys-alphanumeric-key>",
    "project": {
      "host": "<gitlab-host>",
      "project_id": 1,
      "project_path": "<group/project>",
      "project_display_name": "<display-name>",
      "default_branch": "<branch>"
    },
    "source": {
      "prd_path": "docs/prds/<prd>.md",
      "prd_commit_sha": "<40-char-sha>",
      "prd_blob_url": "<commit-pinned-url>",
      "prd_mr_url": "<merged-prd-mr-url>"
    },
    "workspace": {
      "board": "gitlab-p<id>",
      "checkout": "/workspace/projects/p<id>-<slug>",
      "worktree": "/workspace/projects/worktrees/p<id>/<run_key>",
      "branch": "<one-shared-branch>",
      "target_branch": "<default-branch>"
    },
    "origin": {
      "platform": "feishu",
      "message_id": "<om_xxx>",
      "chat_id": "<oc_xxx>",
      "thread_id": "<omt_xxx-or-null>",
      "chat_type": "<group-or-p2p>",
      "initiator_open_id": "<ou_xxx>"
    }
  },
  "stage": "<controlled-stage>",
  "iteration": 1,
  "idempotency_key": "<run_key>:<stage>:<iteration>:work",
  "parent_card_id": "<previous-completed-card>",
  "assignee": "<role-profile>",
  "skills": ["<role-skill>", "glab"],
  "resume_answer": null,
  "resumed_from_card_id": null
}
```

## 完成 metadata v3

`kanban_complete(metadata=...)` 必须提交与
`schemas/card-completion.schema.json` 一致的扁平对象：

- 必填 `protocol_version=hollysys-controller/v1`、`run_key`、`stage`、
  `iteration`、`outcome` 和卡片中的全部项目/workspace/PRD 身份。
- `outcome` 只允许 `pass|fail|scope_gap|cancelled`。
- `scope_gap` 必须同时给出
  `scope_gap_target=spec-write|plan-write|tasks-write` 和非空 `issues`。
- 文档 review 的 pass 必须给完整排序后的 `artifact_paths`、
  `artifact_digest`、`review_commit_sha`。
- test/code-review 的 pass 必须给当前 `mr_iid`、`mr_url`、`head_sha`。
- 不得包含 `next_card_ids`、continuation、`live_reconcile_required`、人类
  resolution 或 merge 专属字段；Schema 对额外字段使用 `additionalProperties=false`。

Hermes 官方完成入口保存 free-form metadata，不会替本部署强制 v3。Worker 必须先
自检；Controller 会在卡片 done 后再次严格验证，非法 metadata 不推进并创建同阶段新
attempt，最多自动重试两次。

## 执行协议

1. `kanban_show()` 读取完整卡片。要求 `created_by=hollysys-controller`，并验证 JSON
   中的 run/stage/iteration/assignee/parent 与当前卡一致。
2. 只在卡片给出的共享 worktree、branch 和单一 MR 工作；GitLab API 操作使用锁定
   `glab`，本地状态使用常规 `git`。
3. Controller 已在发卡前对账；不要例行 fetch/pull。只有 ref 缺失、已证实头不一致
   或 push 被拒绝时才能 fetch，并记录原因。
4. 长任务定期 `kanban_heartbeat(note=...)`。详细工件和证据写入 GitLab；
   Kanban completion 只保存摘要、结构化事实与链接。
5. 正常完成前先构造并自检完整 metadata，然后退订当前卡并立即完成。若完成失败且卡
   仍 in-flight，必须立刻恢复同一订阅，再修正 metadata。
6. 不创建下一卡、不判断整体门禁、不执行合并；Controller 监听完成事件并重读
   Kanban+GitLab后决定下一步。

退订命令：

```text
hermes kanban --board <board> notify-unsubscribe <card-id> \
  --platform feishu --chat-id <chat-id> [--thread-id <thread-id>]
```

## 人类阻塞

普通缺陷用 fail/scope_gap；依赖等待不得伪装成人类问题。只有
`needs_input|capability|transient` 使用 `kanban_block`，并在 block 前保留订阅、
追加一条幂等评论：

```yaml
[human-block:v1]
block_id: <run-key>:<card-id>:<run-id>
kind: <needs_input-or-capability-or-transient>
summary: <发生了什么>
evidence: [<脱敏证据>]
question: <只需回答的一个问题>
options: [<A>, <B>]
required_action: <具体动作>
resume_check: <可验证条件>
```

reason 不超过 160 字符，群聊/话题以原发起人的 `<at>` 开头，并明确：
`处理阻塞 <run-key> <card-id> <答案/已完成动作>`。不得发送 token、密码或原始
敏感日志。Worker 不发飞书、不自行 unblock、不创建恢复卡。Dispatcher 将人类答复
交给 `hollysysctl resolve`；Controller 验证 origin、block 评论和消息幂等后创建新的同阶段
attempt，并以 `outcome=cancelled` 结束旧 blocked 尝试。
