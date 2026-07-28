---
name: hollysys-test
description: 当 Kanban 卡要求独立测试时，验证唯一交付 MR 的准确当前 head 并发布可复现证据。
version: 2.0.0
---

# 测试交付头提交

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 明确要求的常规只读 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是准确工作副本。Controller 已在发卡前对账；不要例行 fetch/pull。只在缺少 ref 或已证实头不一致时 fetch，并记录原因。
- PRD 存在遗漏或歧义，本身不构成阻断性问题。应依次根据明确的验收条件/约束、当前仓库行为和约定、已批准的上游产物、兼容性/安全性作出判断，并选择最小且可逆的范围。若决策影响用户可见的范围/验收、公共接口、数据/迁移、安全/权限、兼容性、恢复/回滚或必需的测试/门禁，则属于关键决策。将每项关键决策写入幂等门禁 MR 评论的 `## 关键自主决策` 章节，并把该评论 URL 写入完成元数据的 `gitlab_urls`；如果没有关键决策，在已有门禁评论中填写 `无`，不要另外发布空评论。单项测试条件不可用按下文结构化跳过，不阻塞；只有完全无法读取交付对象/发布门禁，或证据冲突且不存在安全判定依据时才上报。
- 真正需要人类时，严格执行卡片中的“人类阻塞协议”：写入幂等 `[human-block:v1]` 评论，再 `kanban_block`；Controller outbox 负责原渠道通知，不得自行发飞书、管理订阅、unblock 或创建恢复卡。

1. 调用 `kanban_show()`；要求 `created_by=hollysys-controller`，并验证卡片 JSON 的 v2 protocol、mode、run/stage/iteration/assignee/parent、项目、worktree、分支、MR 和全部冻结基线。开始测试前读取 GitLab 当前 MR head。
2. 阅读所有已批准的 SPEC/PLAN/TASKS 及完整 diff。建立需求/任务到测试的覆盖清单。
3. 运行所有当前条件允许且与变更相称的测试：变更区域测试、必需的集成/契约测试、静态检查和流水线状态检查。不得修改代码或测试文件。先实际预检再判断条件是否具备，不能因为测试费时、可能失败或需要准备就跳过。
4. 若浏览器、专用硬件或外部系统等必要测试条件确实不可用，仍执行所有可用的静态、单元和契约检查，然后使用 `outcome=pass,test_disposition=skipped_unavailable`，填写具体 `skip_reason`、预检与替代检查 `verification`，并把未执行测试写入非空 `residual_risk`。环境暂时失败但可安全自动恢复时应先重试；权限/凭据缺失仍按人类阻塞协议处理。
5. 条件具备时使用 `test_disposition=executed`。任何阻塞交付的实现、覆盖或冻结基线违规都使用带非空 findings 的 `fail`；不得要求回改上游工件。非阻塞上游问题写入残余风险。
6. 发布结论前立即重读 MR head。若已变化，以 `outcome=cancelled` 完成本次陈旧尝试，让 Controller 从 test 重派；不得发布 pass。否则发布绑定准确 `head_sha`、当前 card ID 和 `test_disposition` 的幂等 v5 `test` 评论。
7. 使用严格 v6 metadata 完成，必含完整上下文；pass/fail 都必须将 `head_sha`、
   `mr_iid`、`mr_url` 绑定到当前已测试头，fail 给非空 issues。Controller 会继续让
   code-reviewer 独立审查同一 head，再汇总两份结论；tester 不自行退回 coder。
   不得解决问题、push、创建卡片/MR 或合并。
