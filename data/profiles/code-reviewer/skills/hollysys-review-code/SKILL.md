---
name: hollysys-review-code
description: 独立审查 v4 绑定 Delivery 的准确 current head 并发布 completion Gate。
version: 4.0.0
---

# 审查交付 head

- 只读 Controller 指定 worktree、branch、绑定 MR/head 和冻结基线；不得修改、push、
  创建/选择 MR。GitLab 操作只用锁定的 `glab`。

1. 通过 `hollysysctl card-context --card-id "$HERMES_KANBAN_TASK"` 获取受信 v4 上下文，
   保存到其 `scratch_dir`，核对 expected/current head。
2. 独立于 tester 结论审查完整 diff：现有 MES 复用、正确性、回归、错误路径、测试、
   数据/事务/迁移、权限、兼容性、可维护性，以及真实规模下的性能。不得用假设的
   互联网级攻击或流量制造阻塞 finding。
   必须以 `repository_base_sha` 拒绝绿地式平行框架或重复实现已有能力。
3. 可执行问题用行内讨论；阻塞交付问题用 `fail`。发布前重读 head，变化则
   `cancelled`。
4. pass 时发布绑定 card/head 与冻结 TASKS paths/commit/digest 的幂等
   `implementation_completion` Gate，引用真实 reviewer、requirement IDs、contract
   refs 和精确 note URL；fail 不得伪造 approved Gate。
5. 用 `completion-template` 生成 completion v8，只填写审查/gate/issue/risk，不加入
   authoring-only repository evidence。通过 `validate-completion` 后完成卡片并立即
   结束。
