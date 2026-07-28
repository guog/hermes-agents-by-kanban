---
name: hollysys-dispatch-kanban
description: 当收到正式 PRD 交付启动、状态查询、阻塞恢复或控制器异常时，通过 hollysysctl 与确定性 Controller 交互。
version: 1.0.0
---

# Hollysys Controller 命令入口

本 Skill 只负责自然语言到 `hollysysctl` 的可靠转换。正式卡片、门禁、重试、GitLab
head 对账和合并由无 LLM 的 Controller 负责。

## 禁止事项

- 不调用 `hermes kanban create/link/promote/unblock/complete` 塑造或推进正式流程。
- 不创建 continuation、gate、merge 或恢复卡。
- 不凭 MR 评论、聊天记忆或卡片标题自行判断下一阶段。
- 不使用 Dispatcher GitLab Reporter 身份写评论、改 MR 或合并。
- 不绕过 Controller 直接向 worker 发任务。

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
active_card、board 和 worktree 精简展示给原会话。命令失败时原样保留错误类别，
但不得回显 token、环境变量值或原始敏感日志。

## 状态

收到 `状态 <run_key>` 时执行：

```bash
hollysysctl status --run-key '<run_key>'
```

展示 `phase`、active card/status、attempts、MR/head、gates 和 blocked 摘要。
Controller 返回 `reconciling` 时明确说正在基于 Kanban+GitLab复算，不猜下一阶段。

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

`block_id` 必须来自 `hollysysctl status` 返回的阻塞事实或 Controller 异常提示；不得
根据 run/card 自行编造。Controller 会验证 root origin、匹配的
`[human-block:v1]` 评论、消息幂等和卡片状态，再创建新的同阶段 attempt。命令拒绝
错误 sender/chat/thread、重复或不匹配的 block 时，只解释拒绝原因，不直接改 Kanban。

## 健康与异常

Controller 不可用或出现 Controller exception 时先执行：

```bash
hollysysctl health
hollysysctl status --run-key '<run_key>'
```

只报告返回的事件游标、最近对账、outbox、GitLab/Kanban 连通性和异常卡。若需要
管理员动作，给出一个最小、可验证的动作；修复后重新查询状态。不得用人工创建下一卡
代替恢复 Controller。
