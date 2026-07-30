---
name: hollysys-review-spec
description: 在 v4 受信上下文中确定性校验并独立审查 SPEC。
version: 4.0.0
---

# 审查 SPEC

- 只读 Controller 指定的 worktree、branch、绑定 MR 和准确 head；不得编辑、push、
  创建/选择 MR 或修改冻结上游。
- GitLab 操作只用锁定的 `glab`。只有权限、凭据、能力缺失、不安全重试或破坏性授权
  才能 `kanban_block`；业务缺陷使用 completion `fail`。

1. 执行 `hollysysctl card-context --card-id "$HERMES_KANBAN_TASK"`，保存到响应给出的
   `scratch_dir`。验证 protocol v4、stage、iteration、context digest、expected head、
   Delivery 和冻结基线。
2. 执行 `hollysysctl validate-artifact --card-id "$HERMES_KANBAN_TASK"`，把短 JSON
   中的 validator version、input/result digest 和稳定 error codes 作为共同确定性
   证据。结果不是 `ok=true` 时不得 pass。
   必须以 `repository_base_sha` 拒绝把既有产品当绿地、平行框架或重复实现已有能力。
3. 枚举排序后的 SPEC，核对准确 PRD 与 `repository_base_sha` 上的真实路径和已有
   MES 能力；审查覆盖、可测试性、边界、兼容性、依赖、假设与实现泄漏。
4. 对阻塞 PLAN 的缺陷给出 SPEC 内可执行 findings 并选择 `fail`；非阻塞问题作为
   residual risk。发布绑定当前 card/head、完整 artifact paths/digest/commit 的幂等
   gate 评论。
5. 用 `hollysysctl completion-template ... --outcome pass|fail` 生成 completion v8，
   只填写审查结论、gate URL、决策和风险，不加入 authoring-only repository evidence，
   不修改 Controller 生成的确定性字段。通过 `validate-completion` 后调用
   `kanban_complete`，成功后立即结束。
