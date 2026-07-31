# 飞书命令与通知模板

所有人类可见回复使用飞书 Markdown 富文本。回复必须回到原 `message_id` 和原
`thread_id`；是否添加 `<at user_id="<initiator_open_id>"></at>` 完全沿用 Controller
当前规则。发送前用 `run_key + event` 查询既有消息，避免恢复时重复通知。

统一格式：

- 第一行使用 `**<图标> <中文结论>**`，先给人类结论；
- 字段使用 `**字段：** 值`，ID、Card、SHA 使用行内代码；
- 状态值保留协议原值并追加中文解释，例如 `pass（通过）`；
- findings、决策和风险只展示最多三条简短中文摘要；超长原文以省略号收敛，
  不发送审查过程或原始敏感日志；
- MR、审查评论、流水线、Job 和提交 URL 使用能说明目标的 Markdown 链接，例如
  `查看 MR !58`、`查看 MR !58 审查记录`，禁止使用“链接 1”“证据 2”；
- 禁止重新拼接 `run=... stage=...` 机器格式或直接输出 JSON。

## 人类命令

```text
实现 PRD <精确 PRD blob/raw URL> <已合并 PRD MR URL>
状态 <run_key>
处理阻塞 <run_key> <kanban-card-id> <答案或已完成动作>
```

缺少任一 URL、URL 不属于同一项目或仓库身份不明确时，只询问缺失信息，不创建 run。

## 无法读取项目

```markdown
**❗ 无法确认 GitLab 项目**

**原因：** 项目不存在或当前身份无访问权限
**需要操作：** 请核对两个 URL 和访问权限
```

只有项目已确认存在而 PRD 路径不存在时才使用：

```markdown
**❗ PRD 文件不存在**

**文件：** `<prd-path>`
```

归档项目使用：

```markdown
**⛔ 项目已归档，不允许修改**

**项目：** `<project-path>`
```

## 已接受

```markdown
<at user_id="<initiator_open_id>"></at> **ℹ️ 已受理 PRD 自动交付**

**任务 ID：** `<run_key>`
**项目：** <display-name>
**仓库：** `<group/project>`
**PRD：** `<path>@<short-sha>`
**分支：** `<branch>`
**阶段：** spec-write（编写 SPEC）
**Agent：** SPEC Writer
**Card：** `<card-id>`
```

同一版本已有活跃 run 时回复当前权威状态；已有 merged MR 时回复完成结果，不创建新 run。

## 状态查询

```markdown
**ℹ️ 自动交付当前状态**

**任务 ID：** `<run_key>`
**阶段：** <stage（中文解释）>
**状态：** <run-state（中文解释）>
**Agent：** <agent>
**Card：** `<card-id>`
**阶段轮次：** <n/3，文档 write/review 阶段显示>
**执行尝试：** <attempt/attempt-limit，仅非文档阶段按需显示>
**代码修改：** <n/5，仅 CODE 阶段显示>
**下一步：** <Controller 返回的真实 next action>
```

状态回复只能根据 `hollysysctl status-summary` 返回的事实生成，不能根据聊天记忆补充。

## Agent 生命周期

```markdown
<at user_id="<initiator_open_id>"></at> **✅ Tasker Agent 工作已完成**

**任务 ID：** `<run_key>`
**阶段：** tasks-write（拆分 TASKS）
**阶段轮次：** 2/3
**Agent：** Tasker
**Card：** `<card-id>`
**结论：** pass（通过）
**耗时：** 8分54秒
```

标题和 `Agent` 字段必须来自同一个真实 assignee。SPEC、PLAN、TASKS 的一次
write→review 配对只计一个阶段轮次：第一次为 `1/3`，退回重写并再次审查为 `2/3`，
最后一次为 `3/3`。分子是已使用次数，分母是配置上限。Hermes 进程重派不增加阶段轮次；
非文档阶段若必须展示重派信息，字段名使用“执行尝试”，不得混称“轮次”。

## 文档审查未通过

```markdown
<at user_id="<initiator_open_id>"></at> **⚠️ TASKS 第 1/3 轮审查未通过**

**任务 ID：** `<run_key>`
**审查轮次：** 1/3
**下一位 Agent：** Tasker
**后续处理：** 已退回本阶段 Writer 修订；完成后进入下一轮审查。

**主要问题：**
- <一条简短中文问题，保留文件、行号和协议标识>

**相关链接：**
- [查看 MR !58 审查记录](<review-note-url>)
```

## 文档阶段冻结

```markdown
<at user_id="<initiator_open_id>"></at> **✅ PLAN 第 2/3 轮审查通过，工件已冻结**

**任务 ID：** `<run_key>`
**审查轮次：** 2/3
**冻结结论：** 审查通过
**工件摘要：** `<artifact-digest>`

**关键决策：**
- <最多两条简短中文结论>

**残余风险：**
- <最多两条简短中文风险或“无”>

**相关链接：**
- [查看 MR !58](<mr-url>)
- [查看 MR !58 审查记录](<review-note-url>)
```

## 需要人类

```markdown
<at user_id="<initiator_open_id>"></at> **❗ 自动交付遇到阻塞，需要你的处理**

**任务 ID：** `<run_key>`
**阶段：** <stage（中文解释）>
**Agent：** <agent>
**Card：** `<card-id>`
**需要操作：** <一个明确动作>
**回复方式：** 回复本消息并 @dispatcher：处理阻塞 `<run_key>` `<card-id>` `<答案/已完成动作>`

**阻塞摘要：**
- <脱敏摘要>

**判断依据：**
- <脱敏证据>
```

单聊省略 `<at>`。不要要求人类在飞书中发送 token、密码或原始敏感日志。

CODE 已完成第 5 次修改但双门禁仍未同时通过时使用：

```markdown
<at user_id="<initiator_open_id>"></at> **❗ CODE 自动修改次数已用尽**

**任务 ID：** `<run_key>`
**Head：** `<short-head-sha>`
**修改轮次：** 5/5
**Tester：** <pass|fail（中文解释）>
**Code Reviewer：** <pass|fail（中文解释）>
**需要操作：** 请决定调整任务、停止本次交付或通过正式配置扩大预算

**主要问题：**
- <tester 主要意见>
- <code-reviewer 主要意见>
```

## 恢复

Dispatcher 把实际 sender/chat/thread/message 交给 `hollysysctl resolve`；Controller
核对 root origin 和完整 `[human-block:v1]` 评论。

答复被拒绝：

```markdown
<at user_id="<initiator_open_id>"></at> **⚠️ 暂时无法安全恢复自动交付**

**任务 ID：** `<run_key>`
**Card：** `<card-id>`
**还缺：** <一个明确答案、动作或可验证条件>
**回复示例：** <精确回复示例>
```

恢复成功：

```markdown
<at user_id="<initiator_open_id>"></at> **✅ 已记录并恢复自动交付**

**任务 ID：** `<run_key>`
**阶段：** <stage（中文解释）>
**已解决 Card：** `<blocked-card-id>`
**新 Card：** `<new-card-id>`
**验证：** <short-verification>
```

只有原 `initiator_open_id` 在原 `chat_id/thread_id` 的答复可以恢复。重复答复按
`block_id + message_id` 返回当前状态，不创建重复恢复卡。

## 全部完成

```markdown
<at user_id="<initiator_open_id>"></at> **✅ PRD 自动交付完成**

**任务 ID：** `<run_key>`
**项目：** <display-name>
**仓库：** `<group/project>`
**MR：** [!<iid>](<merged-mr-url>)
**Merge SHA：** `<short-merge-sha>`
**结论：** verified（门禁已验证）

**残余风险：**
- <无或脱敏风险>
```
