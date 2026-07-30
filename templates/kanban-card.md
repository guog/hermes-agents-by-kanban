# [<run_key>] <STAGE> iteration <n>

正式工作卡由 Hollysys Controller v4 生成。Worker 不得创建、link、promote、resolve、
unblock 或猜测其他正式卡。

## 受信上下文

先执行：

```sh
hollysysctl card-context --card-id "$HERMES_KANBAN_TASK"
```

该短响应是唯一可交给业务 Agent 的卡片上下文，包含稳定 `source_key`、随机
`run_key/run_generation`、`provenance=fresh_v4`、项目与 PRD 身份、唯一 worktree/
branch、`expected_head_sha`、`context_digest`、安全 `scratch_dir`、冻结基线和可选
Delivery binding。不得读取完整 Kanban 代替此入口，也不得复用父卡或前次 attempt
上下文。

## 执行边界

1. 只在受信 worktree 和 branch 工作。Agent 不创建/发现/选择/切换 MR；首次 SPEC
   push 后由 Spec Writer 调用 `publish-delivery`，其余阶段只使用已绑定 IID。
2. Controller 已在派卡前按真实 Profile 的登录环境、PATH 和 wrapper 对当前项目/
   branch 做准入。不要例行 fetch/pull；只有缺 ref 或已证实漂移时 fetch 并记录原因。
3. `$HERMES_SCRATCH_DIR` 下每个 attempt 使用独立 `scratch_dir`。不得使用安全根外
   临时目录。
4. heartbeat 只保活，不表示进展。进展只由 head、artifact digest、validator、
   Gate 或 completion 等结构化事实更新。
5. Writer/Reviewer 对文档统一调用：

   ```sh
   hollysysctl validate-artifact --card-id "$HERMES_KANBAN_TASK"
   ```

   `ok=false` 或 `tool_unavailable` 时不得声称通过。
6. 完成时从 Controller 生成当前 stage/mode/outcome 的 completion v8：

   ```sh
   hollysysctl completion-template \
     --card-id "$HERMES_KANBAN_TASK" \
     --outcome pass
   ```

   只补充真实业务证据；不得修改 source/run/context/head_before/deterministic checks，
   不得手写全量示例。随后使用 `validate-completion` 校验；只有 `ok=true` 才调用
   `kanban_complete`。成功后立即结束，不得继续调用模型或业务工具。
7. Worker 不创建下一卡、不做 Controller 路由、不合并。Controller 只在同一
   checked head 上汇总 TEST 与 CODE REVIEW Gate。

## 人类阻塞

业务遗漏、歧义和普通缺陷不阻塞。只有权限、凭据、能力缺失、不安全重试或破坏性
授权才能先发布幂等 `[human-block:v1]` 评论，再调用 `kanban_block`。评论必须包含
稳定 `block_id`、类别、脱敏证据、一个问题、required action 和可验证 resume check。
Worker 不发飞书、不自行 unblock、不创建恢复卡；只有匹配的人类 `resolve` 能让
Controller 创建新的 run_id/attempt。
