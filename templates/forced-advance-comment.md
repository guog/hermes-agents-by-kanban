HOLLYSYS-FORCED-ADVANCE: v=1 run=<run_key> phase=<spec|plan|tasks> review_limit=3 task=<finalization-card-id>

## 强制收敛

- 第三次 review 卡片：<card-id>
- 第三次 review 评论：<gitlab-note-url>
- 仓库基线：<repository_base_sha>
- 仓库证据：<inspected paths、existing capabilities、change strategy、reuse decisions>
- 基线 commit：<40-char-sha>
- 冻结路径：<sorted-paths>
- 工件 digest：<sha256>

## 最终解释与取舍

| 决策 ID | 第三次 finding 或歧义 | 最终处理 | 依据 | 影响与可逆方式 |
| --- | --- | --- | --- | --- |
| `<stable-id>` | <finding> | <fix-or-explicit-tradeoff> | <safety/acceptance/rule/repository/compatibility evidence> | <impact/rollback> |

将每条“最终处理 + 依据 + 影响与可逆方式”的简要摘要同步写入 completion 顶层
`key_decisions` 和 `forced_advance.key_decisions`，两者必须完全一致且至少一条。

## 未解决 findings

- <finding-or-none>

## 残余风险

- <risk, impact, mitigation, and reversible action-or-none>

同一 run、phase、finalization card 重试时先按首行稳定 marker 查找并更新原评论，
不得重复发布。
