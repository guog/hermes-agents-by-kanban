---
name: hollysys-analyze-tasks
description: 用与 Writer 相同的 v4 validator 审查 TASKS 与 implementation-entry Gate。
version: 4.0.0
---

# 审查 TASKS

- 只读 Controller 指定 worktree、branch、绑定 MR/head 和冻结基线；不得编辑、push、
  创建/选择 MR。GitLab 操作只用锁定的 `glab`。
- 只审查 `card-context.run.artifact_scope` 中当前 PRD 的 TASKS。其他 PRD 工件不得进入
  `artifact_paths` 或 findings；偶然发现的问题只作另开 run 的观察。

1. 用 `hollysysctl card-context --card-id "$HERMES_KANBAN_TASK"` 获取受信上下文并保存
   到其 `scratch_dir`。
2. 使用与 Writer 相同的
   `hollysysctl validate-artifact --card-id "$HERMES_KANBAN_TASK"`。确定性结果不是
   `ok=true` 时必须 fail；不得自行实现另一套 DAG 校验或伪造 validator evidence。
   必须以 `repository_base_sha` 拒绝绿地式平行框架或重复实现已有能力。
3. 核实 SPEC/PLAN/TASK 键、真实目标路径与现有能力、动作、DAG、波次、需求覆盖、
   验收和测试。只有 coder 无法安全开工的缺陷才 fail。
4. pass 时发布绑定当前 card/head、TASKS paths/commit/digest 的幂等
   `implementation_entry` Gate，引用真实 requirement IDs/contract refs 和精确 note
   URL；fail 给出 TASKS 内可执行 findings。
5. 用 `completion-template` 生成 pass/fail completion v8，保持 Controller 生成的
   deterministic checks 原样，只填写真实 reviewer/gate/decision/risk。人类可见的
   `issues`、`key_decisions`、`residual_risk` 必须使用简短中文，单项不超过 120 个
   字符；文件路径、行号、需求 ID、表名和协议标识保持原样，不写审查过程复述。通过
   `validate-completion` 后完成卡片并立即结束。
