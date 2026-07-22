# 飞书命令与通知模板

所有回复必须回到原 `chat_id` 和原话题 `thread_id`；群聊或话题回复必须 `@initiator_open_id`，单聊直接回复。发送前用 `run_key + event` 查询既有消息，避免恢复时重复通知。

## 人类命令

```text
实现 PRD <精确 PRD blob/raw URL> <已合并 PRD MR URL>
状态 <run_key>
暂停 <run_key>
继续 <run_key>
重试 <kanban-card-id>
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

```text
自动交付已暂停，需要人类处理。
run=<run_key> stage=<stage> card=<card-id>
原因=<concise-reason>
需要=<specific-action>
GitLab/Kanban=<url-or-command-hint>
```

## 全部完成

```text
PRD 自动交付完成。
run=<run_key> project=<display-name> (<group/project>)
mr=<merged-mr-url> merge=<merge-sha>
验证摘要=<short-summary>
永久链接=<mr-final-comment-url>
残余风险=<none-or-links>
```
