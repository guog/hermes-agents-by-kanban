# 飞书命令与通知模板

所有回复必须回到原 `chat_id` 和原话题 `thread_id`；群聊或话题回复必须 `@initiator_open_id`，单聊直接回复。发送前用 `run_key + event` 查询既有消息，避免恢复时重复通知。

## 人类命令

```text
实现 PRD <精确 PRD blob/raw URL> <已合并 PRD MR URL>
状态 <run_key>
暂停 <run_key>
继续 <run_key>
处理阻塞 <run_key> <kanban-card-id> <答案或已完成动作>
取消 <run_key>
```

缺少任一 URL、URL 不属于同一项目或仓库身份不明确时，只询问缺失信息，不创建 run。

## 无法读取项目

```text
无法确认 GitLab 项目：项目不存在或当前身份无访问权限。请核对两个 URL 和权限。
```

只有项目已确认存在而 PRD 路径不存在时才使用：

```text
文件不存在：<prd-path>
```

归档项目使用：

```text
项目已归档，不允许修改：<project-path>
```

## 已接受或恢复

```text
已接受 PRD 自动交付。
run=<run_key> project=<display-name> (<group/project>) prd=<path>@<short-sha>
branch=<branch> mr=<draft-mr-or-pending> stage=<stage>
```

同一版本已有活跃 run 时回复当前状态；已有 merged MR 时回复完成结果，不创建新 run。

## 需要人类

Kanban notifier 的首条 reason 必须控制在 160 字符内，并在群聊/话题中真实 mention 发起人：

```text
<at user_id="<initiator_open_id>"></at> 自动交付在 <stage> 暂停：<一个问题或动作>。
请回复本消息并 @dispatcher：处理阻塞 <run_key> <card-id> <答案/已完成动作>
```

单聊省略 `<at>`。不要要求人类在飞书中发送 token、密码或原始敏感日志。

Dispatcher 收到答复并核对完整 `[human-block:v1]` 评论后，按需要逐项引导：

```text
<at user_id="<initiator_open_id>"></at> 我还不能安全恢复 <card-id>。
还缺=<一个明确答案、动作或可验证条件>
请回复=<精确回复示例>
```

答复已验证并完成持久交接后：

```text
<at user_id="<initiator_open_id>"></at> 已记录并恢复自动交付。
run=<run_key> resolved=<blocked-card-id> stage=<stage> next=<new-card-id>
验证=<short-verification>
如果再次遇到人类阻塞，我会继续在本话题 @你。
```

只有原 `initiator_open_id` 在原 `chat_id/thread_id` 的答复可以恢复。其他成员的意见可记录为评论，但必须请原发起人确认；重复答复按 `block_id + message_id` 返回当前状态，不创建重复恢复卡。

## 全部完成

```text
PRD 自动交付完成。
run=<run_key> project=<display-name> (<group/project>)
mr=<merged-mr-url> merge=<merge-sha>
验证摘要=<short-summary>
永久链接=<mr-final-comment-url>
残余风险=<none-or-links>
```
