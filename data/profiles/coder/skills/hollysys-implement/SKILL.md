---
name: hollysys-implement
description: 当 Kanban 卡要求实现或修复时，在唯一共享 MR 完成冻结 TASKS 及测试。
version: 2.0.0
---

# 实现 PRD

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 为检查 worktree、commit 和 push 而明确要求的常规 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是唯一可编辑副本。Controller 已在发卡前对账；不要例行 fetch/pull。只有缺少 ref、已证实头不一致或 push 被拒绝时才能 fetch，并记录原因。
- 这是既有企业 MES 产品的客户定制，不是从零开发。实现必须以
  `repository_base_sha` 的基础代码、文档、配置和测试为起点，优先复用现有框架、
  模块、组件和约定，并在原有功能上扩展或修改。不得另起平行框架、整段重写可复用
  模块，或用新实现绕开已有业务链路。
- PRD 存在遗漏、歧义或矛盾，本身不等于阻塞。不得修改任何冻结工件；应依次根据安全、明确的验收条件、具体规则、仓库契约、兼容性和最小可逆范围作出判断。若决策影响用户可见范围/验收、公共接口、数据/迁移、安全/权限、兼容性、恢复/回滚或必需门禁，则属于关键决策。完成前，使用 `/opt/fleet/templates/decision-comment.md` 对账一条幂等的交付 MR 评论，记录本卡关键决策并将 URL 写入 `gitlab_urls`；若没有关键决策，不发布空评论。
- 真正需要人类时，严格执行卡片中的“人类阻塞协议”：写入幂等 `[human-block:v1]` 评论，再 `kanban_block`；Controller outbox 负责原渠道通知，不得自行发飞书、管理订阅、unblock 或创建恢复卡。

1. 调用 `kanban_show()`；要求 `created_by=hollysys-controller`，并验证卡片 JSON 的 v2 protocol、mode、run/stage/iteration/assignee/parent、项目、worktree/分支、PRD、交付 MR、冻结基线和可选 `repair_context`。绝不得创建另一个 checkout、分支或 MR。
2. 写入前，对账当前分支、commit、MR、流水线和已有部分实现；按 TASKS 引用路径
   读取相关现有代码、调用链、数据契约、配置、文档与回归测试，确认每项工作是
   `reuse|modify|extend|create`。若 TASKS 的路径或现状判断有误，在 CODE 阶段按真实
   仓库作最小可逆适配，不回改冻结文档。
3. 在现有落点添加或更新必需代码和测试。只有确无合适扩展点时才新增结构，并记录
   证据与兼容影响。遵循仓库约定，以最小连贯单元 commit/push，保留无关改动。
4. 运行与改动相称的格式化、静态检查、单元测试、集成/契约检查，并在同一个 MR 描述中记录准确的命令和结果。
5. 按上述决策层级解决上游遗漏、歧义或矛盾，不得回改冻结工件。`repair_context.kind=code_gate_failure` 时核对其中同一 `head_sha` 的 tester 与 code-reviewer 卡片和全部 findings，并按 `code_modification=n/code_modification_limit` 在一次连贯修改中逐项处理两者意见；不得只修其中一方。Controller 最多派发 5 次修改。`frozen_artifact_violation` 时先精确恢复冻结 blob，并保留上下文中已有代码 findings，再继续实现。
6. 覆盖所有 TASK 且自测通过后，更新 `/opt/fleet/templates/mr-description.md` 所定义的元数据，并将现有 Draft MR 标记为 ready。代码修复时保持 ready，除非 GitLab 策略要求变更期间设为 draft。
7. 完成时提交严格 v6 metadata，必含完整上下文、MR/head、验证，以及绑定
   `repository_base_sha` 的 `repository_evidence`（实际检查路径、现有能力、变更类型
   和复用决策）。不得包含 continuation/下一卡/merge 字段，不得自审、创建卡片或合并。
