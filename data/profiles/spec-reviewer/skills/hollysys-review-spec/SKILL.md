---
name: hollysys-review-spec
description: 当 Kanban 卡要求审查 SPEC 时，验证完整性与可测试性并发布绑定摘要的门禁。
version: 2.0.1
---

# 审查 SPEC 集合

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 明确要求的常规只读 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是准确工作副本。Controller 已在发卡前对账；不要例行 fetch/pull。只在缺少 ref 或已证实头不一致时 fetch，并记录原因。
- PRD 遗漏、歧义或矛盾不得阻塞或要求回改 PRD。检查 writer 是否按安全、明确验收、具体规则、仓库契约/兼容性和最小可逆范围形成了自洽决定；只有使 PLAN 无法安全开展的 SPEC 缺陷才 fail，其余写入 residual risk 后 pass。
- 只有权限、凭据、环境/能力缺失、自动重试不再安全或破坏性动作待授权时才允许人类阻塞；不得因业务内容调用 `kanban_block`。

1. 调用 `kanban_show()`；要求 `created_by=hollysys-controller`，并验证卡片 JSON 的 v2 protocol/mode/run/stage/iteration/assignee/parent、项目、worktree、分支、固定 PRD blob、冻结基线和共享 MR。
2. 枚举完整且排序后的 `spec-<key>.md` 集合。读取准确 PRD、仓库
   `repository_base_sha` 上被引用的现有代码/文档和 SPEC 模板；检查“现有行为、
   PRD 差异、期望结果”是否有真实仓库证据。把把既有 MES 当成绿地项目、重复定义
   已有能力、忽略兼容行为或引用不存在的扩展点视为实质性缺陷。同时检查完整覆盖、
   可测试性、边界、假设、成功标准、依赖、矛盾和实现细节泄漏。
3. 在审查 commit 上计算排序后的 path/blob 摘要，使用 gate 模板发布幂等 v5 评论，包含完整路径、`artifact_digest`、`artifact_commit_sha`、当前 card ID 和本评论 URL。
4. 阻塞性 SPEC 缺陷使用 `fail` 并给出 writer 可在 SPEC 内完成的精确动作；第几次 review 以卡片 iteration/历史为准，不自行决定回退。纯格式和可延后改进不得 fail。
5. 使用 `protocol_version=hollysys-controller/v2,mode=normal` 的严格 v6 metadata 完成。pass 和 fail 都绑定 paths/digest/artifact commit；pass 填 `baseline_disposition=reviewed`，fail 必须给非空 issues；两者都把 gate URL 写入 `gitlab_urls`，并将门禁评论中的关键自主决策摘要写入 `key_decisions`。不得编辑产物、push、创建卡片或合并。
   审查必须核实仓库证据，但 completion metadata 不得包含仅 authoring pass 可用的
   `repository_evidence`；仓库核查结果写入 gate 评论、`verification` 和
   `key_decisions`。
