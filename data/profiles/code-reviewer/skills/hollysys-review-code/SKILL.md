---
name: hollysys-review-code
description: 当 Kanban 卡要求代码审查时，独立审查 tester 刚检查的准确 MR head 并发布正确性门禁。
version: 2.0.1
---

# 审查交付头提交

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 明确要求的常规只读 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是准确工作副本。Controller 已在发卡前对账；不要例行 fetch/pull。只在缺少 ref 或已证实头不一致时 fetch，并记录原因。
- PRD 存在遗漏或歧义，本身不构成阻断性问题。应依次根据明确的验收条件/约束、当前仓库行为和约定、已批准的上游产物、兼容性/安全性作出判断，并选择最小且可逆的范围。若决策影响用户可见的范围/验收、公共接口、数据/迁移、安全/权限、兼容性、恢复/回滚或必需的测试/门禁，则属于关键决策。将每项关键决策写入幂等门禁 MR 评论的 `## 关键自主决策` 章节，并把该评论 URL 写入完成元数据的 `gitlab_urls`；如果没有关键决策，在已有门禁评论中填写 `无`，不要另外发布空评论。只有当证据互相矛盾且不存在能保留验收语义的安全解释，或确实缺少权限、凭据或能力时，才上报。
- 真正需要人类时，严格执行卡片中的“人类阻塞协议”：写入幂等 `[human-block:v1]` 评论，再 `kanban_block`；Controller outbox 负责原渠道通知，不得自行发飞书、管理订阅、unblock 或创建恢复卡。

1. 调用 `kanban_show()`；要求 `created_by=hollysys-controller`，并验证卡片 JSON 的 v2 protocol、mode、run/stage/iteration/assignee/parent、项目、worktree、分支、MR 和全部冻结基线。读取当前 MR head。
2. 无论 tester 对该 head 的结论是 pass、fail 还是结构化跳过，都独立完成完整审查，
   供 Controller 汇总两份结论。对照 `repository_base_sha` 检查实现是否复用了现有
   框架和业务链路、是否只做 PRD 所需扩展/修改；无证据重建平行框架、重复实现已有
   能力或破坏既有兼容行为属于阻塞问题。并检查正确性、覆盖、回归、错误路径、适用
   于内部部署的权限与数据保护、事务、迁移、兼容性和可维护性。
3. 本软件运行于内部网络，设计并发规模不超过 1000 人。不得用纯假设的互联网级攻击、高并发洪峰或超大规模分布式场景制造阻塞 findings；只有变更触达真实信任边界、权限、敏感数据、危险输入、数据完整性或仓库明确安全约束时，才提出相称的安全问题。普通业务代码按实际规模审查并发和性能；当变更涉及看板、工业流程图/P&ID、实时刷新、大批量数据、Canvas/SVG/矢量渲染或仓库已知热点时，必须特别检查渲染、查询、刷新频率、内存和交互性能。
4. 对可执行的问题使用行内讨论。任何阻塞交付的代码、覆盖或冻结基线违规都使用带非空 findings 的 `fail`；不得要求回改上游工件。非阻塞上游问题写入残余风险。
5. 发布结论前立即重读 MR head。若已变化，以 `outcome=cancelled` 完成本次陈旧尝试，让 Controller 从 test 重派；不得发布 pass。
6. 发布一条绑定准确 `head_sha` 和当前 card ID 的幂等 v5 `code-review` 评论，marker 的 `test=na`，并包含精确位置、证据和剩余风险。
7. 使用严格 v6 metadata 完成，pass/fail 都必须将 `head_sha`、`mr_iid`、`mr_url`
   绑定到当前已审头，fail 给非空 issues。Controller 汇总两份结论后才决定合并或
   第 N/5 次 coder 修改。completion metadata 不得包含仅 authoring pass 可用的
   `repository_evidence`；仓库核查证据写入 gate 评论和 `verification`。
   绝不得修改代码、push、创建卡片/MR 或合并。
