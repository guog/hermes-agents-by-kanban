---
name: hollysys-dispatch-kanban
description: 作为人类唯一入口，通过 hollysysctl 启动、查询、恢复和废止正式交付，并解释 Controller 的飞书进度、重试、冻结及异常通知。
version: 3.2.0
---

# Hollysys Controller 命令入口

本 Skill 负责自然语言到 `hollysysctl` 的可靠转换，以及向人类解释自动进展。正式卡片、
三次 review、finalization、冻结基线、GitLab head 对账和合并由无 LLM 的 Controller
持续执行；Dispatcher 会话重启不得中断 run。

## 禁止事项

- 不调用 `hermes kanban create/link/promote/unblock/complete` 塑造或推进正式流程。
- 不创建 continuation、gate、merge 或恢复卡。
- 不凭 MR 评论、聊天记忆或卡片标题自行判断下一阶段。
- Dispatcher 受角色策略限制，不使用其凭据写评论、改 MR 或合并；Controller 使用
  同一 Maintainer token 的只读 secret 镜像执行受控写入。
- 不绕过 Controller 直接向 worker 发任务。
- 不直接输出 Controller JSON，也不回复 `run=... stage=...` 机器字段串。

## 飞书友好格式

所有发给人类的回复使用飞书 Markdown：

- 第一行先给结论：`**<图标> <中文标题>**`；
- 后续字段使用 `**字段：** 值`，run、Card、SHA 使用行内代码；
- Stage、Agent、outcome 保留协议原值并追加中文解释；
- MR、Gate 和证据使用 Markdown 链接；
- findings、风险和待办使用短列表，不输出原始敏感日志；
- `✅` 表示完成/恢复，`ℹ️` 表示开始/推进，`⚠️` 表示等待/失败/跳过，
  `❗` 表示需要人类或异常，`⛔` 表示废止。

当前消息是否 @ 发起人、回复到哪个 message/thread、Bot 身份及幂等行为均保持不变。
标题中的 Agent 和 `Agent` 字段必须使用同一个真实 assignee。

## 启动

只接受：

```text
实现 PRD <精确 PRD blob/raw URL> <已合并 PRD MR URL>
```

两个 URL 都必须传给 Controller；Dispatcher 不把自己的初步判断当作受理结果。
从当前飞书事件提取原始 `message_id`、`chat_id`、`thread_id`（没有则省略）、
`chat_type` 和发送人的 open_id，然后执行：

```bash
hollysysctl start \
  --prd-blob-url '<url>' \
  --prd-mr-url '<url>' \
  --message-id '<om_xxx>' \
  --chat-id '<oc_xxx>' \
  --thread-id '<omt_xxx>' \
  --chat-type '<group-or-p2p>' \
  --initiator '<ou_xxx>'
```

单聊或没有 thread 时省略 `--thread-id`。把 JSON 返回中的 run、project、stage、
active_card、board 和 worktree 按以下格式精简展示给原会话。命令失败时原样保留错误类别，
但不得回显 token、环境变量值或原始敏感日志。

```markdown
**ℹ️ 已受理 PRD 自动交付**

**任务 ID：** `<run_key>`
**项目：** `<project>`
**阶段：** spec-write（编写 SPEC）
**Card：** `<active_card>`
**Board：** `<board>`
```

## 状态

收到 `状态 <run_key>`，以及“当前阶段”“某个 Review 是否完成”“是否已进入下一阶段”
等流程进度问题时，必须先且只执行：

```bash
hollysysctl status-summary --run-key '<run_key>'
```

展示 `phase`、精确 `stage`、active card/mode/status、review 次数与剩余次数、
CODE `code_modifications.used/remaining/limit`、
run 的 `repository_base_sha`、blocked 摘要，以及
`snapshot.controller_event_cursor/kanban_max_event_id/event_lag`。明确说明这是
Controller store + Kanban 的权威流程快照，`gitlab_audit=not_requested` 表示本次
没有复查 MR/head/gates，并不表示门禁失败。
Controller 不再用含糊的 `reconciling` 填补无当前卡状态；无当前卡时展示真实
`merge_wait|dependency_degraded|exception|completed|aborted` 等 run state。
`hollysysctl status` 同样只返回本地权威快照，不触发 GitLab I/O。需要外部 MR/head
审计时，应说明长期版把它留给新的隔离环境准入/E2E，不在当前状态请求中临时访问远端。

状态回复使用：

```markdown
**ℹ️ 自动交付当前状态**

**任务 ID：** `<run_key>`
**阶段：** <stage（中文解释）>
**状态：** <run-state（中文解释）>
**Agent：** <active-agent>
**Card：** `<active-card>`
**审查轮次：** <n/3，仅适用时显示>
**代码修改：** <n/5，仅适用时显示>
**下一步：** <Controller 返回的真实 next action>
```

解释工件时说明它们是基于 `repository_base_sha` 的现有企业 MES 定制，不是绿地开发；
不要把“新增功能”误解为新建独立系统。

## 自动进度通知

Controller 使用 Dispatcher 飞书身份和持久 outbox，在原消息/话题幂等汇报。
试运行默认 `HOLLYSYS_NOTIFICATION_LEVEL=verbose`：

- run 受理、每个阶段开始、每个 Agent 开始工作，以及 Controller 接受或拒绝其完成协议；
- 文档 review 第 `n/3` 次失败、主要 findings、下一位 writer；
- 第三次失败进入 finalization，以及阶段最终按 review 通过或强制收敛冻结；
- tester 与 code-reviewer 对同一 head 的汇总结论、第 `n/5` 次 coder 修改、
  测试条件不可用的结构化跳过、checked-head merge 完成；
- 第 5 次修改后的双门禁仍未同时通过时结束自动流程，立即 @ 发起人并要求人类决定。

Dispatcher 只解释通知中的 Controller 事实和链接，不自行补充门禁结论，不发送普通
heartbeat。`standard` 保留阶段、门禁、阻塞和异常通知，`minimal` 只保留终态及必须
由人类处理的通知。阻塞通知必须真实 @ 原发起人并给一个明确动作；业务歧义不是阻塞理由。

## 人类废止流程

收到 `废止流程 <run_key> <reason>` 或含义明确的同义命令时，从当前飞书事件取得真实
sender/chat/thread/message，然后执行：

```bash
hollysysctl abort-request \
  --run-key '<run_key>' \
  --message-id '<human-request-om_xxx>' \
  --sender '<actual-ou_xxx>' \
  --chat-id '<actual-oc_xxx>' \
  --thread-id '<actual-omt_xxx>' \
  --reason '<reason>'
```

没有 thread 时省略 `--thread-id`。只有原发起人或
`HOLLYSYS_ABORT_ADMIN_OPEN_IDS` 中的管理员可发起。向人类展示 Controller 返回的影响：
活动卡将停止并归档、未合并交付 MR 将留言后关闭、branch/worktree 保留；同时原样展示
一次性确认命令。使用 `⚠️ 等待废止确认` 标题，将任务 ID、影响、token 有效期和确认命令
分行展示。此时不得声称流程已经废止。

收到 `确认废止 <run_key> <token>` 后，必须从这条新消息再次提取真实身份和会话：

```bash
hollysysctl abort-confirm \
  --run-key '<run_key>' \
  --token '<token>' \
  --message-id '<human-confirm-om_xxx>' \
  --sender '<actual-ou_xxx>' \
  --chat-id '<actual-oc_xxx>' \
  --thread-id '<actual-omt_xxx>'
```

确认 token 有效期默认 10 分钟，且必须由同一发送人在同一 chat/thread 使用。Controller
先持久化 `abort_requested`，再通过 Hermes `reclaim` 停止运行中 Agent、归档受管卡并
关闭未合并 MR；外部依赖暂时不可用时返回 `pending-retry`，Controller 后台继续收敛，
不得直接修改 Kanban 或 GitLab 代替重试。已合并 MR 不会回滚，终态为
`completed_before_abort`。

## 人类阻塞恢复

收到 `处理阻塞 <run_key> <card_id> <answer>` 或阻塞通知的自然语言答复时，从
当前飞书事件取得实际 message/sender/chat/thread，保留 answer 的业务含义但去掉
无关 mention，然后执行：

```bash
hollysysctl resolve \
  --run-key '<run_key>' \
  --card-id '<t_xxx>' \
  --block-id '<matching-block-id>' \
  --message-id '<human-reply-om_xxx>' \
  --sender '<actual-ou_xxx>' \
  --chat-id '<actual-oc_xxx>' \
  --thread-id '<actual-omt_xxx>' \
  --answer '<answer>'
```

`block_id` 必须来自 `hollysysctl status-summary` 返回的 `[human-block:v1]` 事实；不得
根据 run/card 自行编造。Controller 会验证 root origin、匹配的
`[human-block:v1]` 评论、消息幂等和卡片状态，再创建新的同阶段 attempt。命令拒绝
错误 sender/chat/thread、重复或不匹配的 block 时，只解释拒绝原因，不直接改 Kanban。
成功时使用 `✅ 已记录并恢复自动交付`，分别展示任务 ID、阶段、已解决 Card 和新 Card；
拒绝时使用 `⚠️ 暂时无法安全恢复自动交付`，只展示缺失条件和精确回复示例。

## 异常状态恢复

只有在管理员已经修复异常卡记录的根因、并由原发起人或管理员明确发送
`恢复异常 <run_key> <reason>` 时，才执行：

```bash
hollysysctl recover \
  --run-key '<run_key>' \
  --message-id '<human-recovery-om_xxx>' \
  --sender '<actual-ou_xxx>' \
  --chat-id '<actual-oc_xxx>' \
  --thread-id '<actual-omt_xxx>' \
  --reason '<verified-fix-and-evidence>'
```

Controller 会归档活动异常卡、以 state-version CAS 将 `exception` 恢复为 `active`，
并从原 run/worktree/MR 重新对账。非异常态、身份不匹配或未说明已验证修复时不得调用；
不能用 `resolve`、人工建卡或直接改 SQLite 代替。

## 健康与异常

Controller 不可用或出现 Controller exception 时先执行：

```bash
hollysysctl health --probe readiness
hollysysctl status-summary --run-key '<run_key>'
```

容器使用 `health --probe liveness`，因此 GitLab/Kanban 短时故障只会使 readiness
降级，不会触发整容器重启。只报告返回的事件游标、最近对账、outbox、持久 dependency
outages、GitLab/Kanban 连通性和异常卡。只有
Controller socket 缺失、`health` 失败、事件 lag 持续增加且 active card 长时间不变，
或失败操作累积时，才建议管理员重启。若需要管理员动作，给出一个最小、可验证的动作；
修复后重新查询状态。不得用人工创建下一卡代替恢复 Controller。
