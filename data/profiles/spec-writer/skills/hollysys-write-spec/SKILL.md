---
name: hollysys-write-spec
description: 在 Controller 指定的唯一 v4 worktree 和分支上编写、验证并交付 SPEC。
version: 4.0.0
---

# 编写 SPEC

- GitLab 操作只使用锁定的 `glab`；本地检查、commit 和 push 使用受控 `git`。
- 只操作 Controller 给出的 worktree、分支和 Delivery。不得 clone、创建/选择分支、
  创建/选择/切换 MR，或修改 PRD。
- 业务歧义按安全与数据完整性、明确验收、具体规则、仓库兼容性、最小可逆范围决策，
  写入 SPEC 与 MR 描述。只有权限、凭据、能力缺失、不安全重试或破坏性授权才能
  使用 `[human-block:v1]` 和 `kanban_block`。

1. 以当前 `$HERMES_KANBAN_TASK` 为 card ID，先执行
   `hollysysctl card-context --card-id "$HERMES_KANBAN_TASK"`，把输出保存到该响应的
   `scratch_dir`。只信任响应中的 v4 run/source/workspace/head/context/delivery 和
   frozen baseline。
2. 从 `repository_base_sha` 读取 PRD、仓库规则、架构、相邻模块、契约和测试，列出
   已有能力、真实路径、可复用点与 PRD 差异。按模板
   `/opt/fleet/templates/spec-template.md` 写入稳定的
   `docs/prds/<prd>/specs/spec-<key>.md`，定义完整覆盖、边界和可测试期望，不泄漏实现。
3. repair 只处理本阶段 findings；冻结违规先恢复 Controller 指定 blob。保持稳定键，
   不修改冻结上游。
4. 约定式 commit 后 push 当前唯一分支，并核对本地 `HEAD` 与远端 branch head 完全
   相同。首次有效 SPEC push 后，把 MR 描述写入本 card 的 `scratch_dir`，执行
   `hollysysctl publish-delivery --card-id "$HERMES_KANBAN_TASK" --head-sha "$(git rev-parse HEAD)" --description-file <描述文件>`。
   只有 Controller 能创建并绑定 Draft MR；仅 `iteration=1` 且 card-context 的
   `delivery=null` 时调用一次。后续 repair/redispatch 已有 Delivery，绝不再次调用；
   命令失败时不得发现或接管其他 MR。
5. 执行 `hollysysctl validate-artifact --card-id "$HERMES_KANBAN_TASK"`。结果不是
   `ok=true`（包括 `tool_unavailable`）时不得声称通过。
6. 执行 `hollysysctl completion-template --card-id "$HERMES_KANBAN_TASK" --outcome pass`
   生成 completion v8 文件，只补充模板中的 `repository_evidence`、验证和关键决策，不手写完整
   metadata，不改 source/run/context/head_before/deterministic checks。再执行
   `hollysysctl validate-completion --card-id "$HERMES_KANBAN_TASK" --metadata <文件>`；
   只有 `ok=true` 才调用 `kanban_complete`。成功后立即结束，不再调用模型或业务工具。
