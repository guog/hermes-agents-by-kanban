# [<run_key>] <STAGE> iteration <n>

正式工作卡由确定性 Controller 生成；本模板说明卡片 JSON 与执行合同。Worker
不得创建/link/promote/unblock 其他正式卡，也不得猜测下一阶段。

```json
{
  "protocol_version": "hollysys-controller/v2",
  "kind": "work",
  "run": {
    "protocol_version": "hollysys-controller/v2",
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
      "prd_blob_sha": "<40-char-git-blob-sha>",
      "prd_blob_url": "<commit-pinned-url>",
      "prd_mr_url": "<merged-prd-mr-url>"
    },
    "workspace": {
      "board": "gitlab-p<id>",
      "checkout": "/workspace/projects/p<id>-<slug>",
      "worktree": "/workspace/projects/worktrees/p<id>/<run_key>",
      "branch": "<one-shared-branch>",
      "target_branch": "<default-branch>",
      "repository_base_sha": "<40-char-run-start-default-branch-sha>"
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
  "mode": "<normal|finalization>",
  "idempotency_key": "<run_key>:<stage>:<iteration>:<mode>:work",
  "parent_card_id": "<previous-completed-card>",
  "assignee": "<role-profile>",
  "skills": ["<role-skill>", "glab"],
  "frozen_baselines": [
    {
      "phase": "<prd|spec|plan|tasks>",
      "disposition": "<source|reviewed|forced_after_review_limit>",
      "artifact_paths": ["<sorted-path>"],
      "artifact_digest": "<sha256>",
      "artifact_commit_sha": "<40-char-sha>",
      "source_card_id": "<card-id>",
      "decision_urls": [],
      "key_decisions": [],
      "unresolved_findings": [],
      "residual_risk": []
    }
  ],
  "repair_context": {
    "kind": "<review_failure|code_gate_failure|frozen_artifact_violation>",
    "trigger_card_id": "<card-id>",
    "issues": ["<exact-finding>"],
    "review_attempt": null,
    "review_limit": null,
    "related_card_ids": [],
    "head_sha": null,
    "code_modification": null,
    "code_modification_limit": null,
    "frozen_baselines": []
  },
  "resume_answer": null,
  "resumed_from_card_id": null
}
```

## 完成 metadata v6

`kanban_complete(metadata=...)` 必须提交与
`schemas/card-completion.schema.json` 一致的扁平对象：

- 必填 `protocol_version=hollysys-controller/v2`、`run_key`、`stage`、
  `iteration`、`mode`、`outcome`、`prd_blob_sha` 和卡片中的全部
  project/workspace/PRD 身份。
- `outcome` 只允许 `pass|fail|cancelled`；`fail` 必须给非空 `issues`。
- 文档 review 的 pass/fail 都必须给完整排序后的 `artifact_paths`、
  `artifact_digest`、`artifact_commit_sha`；pass 还必须给
  `baseline_disposition=reviewed`。
- test/code-review 的 pass/fail 都必须给当前 `mr_iid`、`mr_url`、`head_sha`。
- spec-write、plan-write、tasks-write、implement 的 pass 必须给
  `repository_evidence`：绑定卡片的 `repository_base_sha`，列出实际检查过的
  现有代码/文档路径、现有能力、`extend_existing|modify_existing|
  extend_and_modify` 变更类型，以及明确的复用决策。不能只写“已检查仓库”。
- test 的 pass/fail 还必须给 `test_disposition=executed|skipped_unavailable`。
  只有测试条件经实际预检确认不具备时才允许 `skipped_unavailable`，此时使用
  `outcome=pass`，并给出非空 `skip_reason`、已执行的可用检查 `verification`
  以及未执行测试带来的 `residual_risk`。
- finalization producer 的 pass 必须给工件证据、MR、`baseline_disposition=
  forced_after_review_limit` 和完整 `forced_advance`，其中包含第三次 review
  卡片/评论、最终决策评论、baseline commit/paths/digest、关键决策、未解决
  findings、residual risks 和 review_limit；嵌套证据必须与顶层字段一致。
- 不得包含 `next_card_ids`、continuation、`live_reconcile_required`、人类
  resolution 或 merge 专属字段；Schema 对额外字段使用 `additionalProperties=false`。

Hermes 官方完成入口保存 free-form metadata，不会替本部署强制 v6。Worker 必须先
自检；Controller 会在卡片 done 后再次严格验证，非法 metadata 不推进并创建同阶段新
attempt，最多自动重试两次。

## 执行协议

1. `kanban_show()` 读取完整卡片。要求 `created_by=hollysys-controller`，并验证 JSON
   中的 run/stage/iteration/mode/assignee/parent、冻结基线和 repair_context
   与当前卡一致。
2. 只在卡片给出的共享 worktree、branch 和单一 MR 工作；GitLab API 操作使用锁定
   `glab`，本地状态使用常规 `git`。
3. Controller 已在发卡前对账；不要例行 fetch/pull。只有 ref 缺失、已证实头不一致
   或 push 被拒绝时才能 fetch，并记录原因。
4. 长任务定期 `kanban_heartbeat(note=...)`。heartbeat 只保活，不触发飞书进度
   通知。详细工件和证据写入 GitLab；
   Kanban completion 只保存摘要、结构化事实与链接。
5. 正常完成前先构造并自检完整 metadata，然后立即完成；Controller 的持久 outbox
   负责业务进度和阻塞通知。
6. 不创建下一卡、不判断整体门禁、不执行合并；Controller 监听完成事件并重读
   Kanban+GitLab后决定下一步。
7. CODE 阶段不会因 tester fail 提前退回 coder；code-reviewer 仍审查同一 head。
   两道门禁都完成后，Controller 汇总 findings。任一未通过才派发下一次 coder
   修改，最多 5 次；第 5 次修改后的双门禁仍未同时通过则结束自动流程并通知人类。

## 人类阻塞

业务遗漏、歧义或矛盾不阻塞，也不得要求回改冻结工件。普通缺陷用 fail；依赖等待
不得伪装成人类问题。只有权限、凭据、环境/能力缺失、自动重试不安全或破坏性操作
待授权时使用 `kanban_block`，并在 block 前追加一条幂等评论：

```yaml
[human-block:v1]
block_id: <run-key>:<card-id>:<run-id>
kind: <permission|credential|environment|unsafe_retry|destructive_approval>
summary: <发生了什么>
evidence: [<脱敏证据>]
question: <只需回答的一个问题>
options: [<A>, <B>]
required_action: <具体动作>
resume_check: <可验证条件>
```

reason 不超过 160 字符，群聊/话题以原发起人的 `<at>` 开头，并明确：
`处理阻塞 <run-key> <card-id> <答案/已完成动作>`。不得发送 token、密码或原始
敏感日志。Worker 不发飞书、不管理通知订阅、不自行 unblock、不创建恢复卡。
Controller outbox 将阻塞通知投递到原会话，Dispatcher 将人类答复
交给 `hollysysctl resolve`；Controller 验证 origin、block 评论和消息幂等后创建新的同阶段
attempt，并以 `outcome=cancelled` 结束旧 blocked 尝试。
