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
- 本任务由父 Agent 完成，不调用 `delegate_task`，也不做全仓库扫描；先按 PRD/SPEC
  引用定位真实模块，单轮最多读取 20 个目标文件。
- 只审查 `card-context.run.artifact_scope` 返回的当前 PRD SPEC。其他 PRD 工件不进入
  `artifact_paths` 或 findings；若偶然发现问题，只能作为另开 run 的观察，不能要求修复。

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
   审查评论；这不是 semantic gate。`gate_phase`、所有 `gate_*`、`contract_refs` 和
   `requirement_ids` 必须保持模板中的 null/空值，评论 URL 只放入 `gitlab_urls`。
5. 用 `hollysysctl completion-template ... --outcome pass|fail` 生成 completion v8，
   只填写审查结论、评论 URL、决策和风险，不加入 authoring-only repository evidence，
   人类可见的 `issues`、`key_decisions`、`residual_risk` 必须使用简短中文，单项不超过
   120 个字符；文件路径、行号、需求 ID、表名和协议标识保持原样，不写审查过程复述。
   不读取完整 schema，不修改 Controller 生成的确定性字段。直接运行
   `validate-completion`，不得用 `execute_code` 包裹；成功后直接调用
   `kanban_complete` 并立即结束。
