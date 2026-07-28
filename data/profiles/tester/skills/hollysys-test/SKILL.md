---
name: hollysys-test
description: 当 Kanban 卡要求独立测试时，验证唯一交付 MR 的准确当前 head 并发布可复现证据。
version: 1.0.0
---

# 测试交付头提交

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 明确要求的常规只读 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是准确工作副本。Controller 已在发卡前对账；不要例行 fetch/pull。只在缺少 ref 或已证实头不一致时 fetch，并记录原因。
- PRD 存在遗漏或歧义，本身不构成阻断性问题。应依次根据明确的验收条件/约束、当前仓库行为和约定、已批准的上游产物、兼容性/安全性作出判断，并选择最小且可逆的范围。若决策影响用户可见的范围/验收、公共接口、数据/迁移、安全/权限、兼容性、恢复/回滚或必需的测试/门禁，则属于关键决策。将每项关键决策写入幂等门禁 MR 评论的 `## 关键自主决策` 章节，并把该评论 URL 写入完成元数据的 `gitlab_urls`；如果没有关键决策，在已有门禁评论中填写 `无`，不要另外发布空评论。只有当证据互相矛盾且不存在能保留验收语义的安全测试判定依据，或确实缺少权限、凭据或能力时，才上报。
- 真正需要人类时，严格执行卡片中的“人类阻塞协议”：确认自己的原渠道订阅仍存在，写入幂等 `[human-block:v1]` 评论，再 `kanban_block`；不得自行发飞书、退订、unblock 或创建恢复卡。正常完成前退订当前卡。

1. 调用 `kanban_show()`；要求 `created_by=hollysys-controller`，并验证卡片 JSON 的 protocol/run/stage/iteration/assignee/parent、项目、worktree、分支、MR 和已批准产物。开始测试前读取 GitLab 当前 MR head。
2. 阅读所有已批准的 SPEC/PLAN/TASKS 及完整 diff。建立需求/任务到测试的覆盖清单。
3. 运行最小但完整且可复现的测试套件：变更区域测试、必需的集成/契约测试、静态检查和必需的流水线状态检查。不得修改代码或测试文件。
4. 实现缺陷使用 `fail`；只有存在证据表明产物有遗漏时才使用 `scope_gap`。
5. 发布结论前立即重读 MR head。若已变化，以 `outcome=cancelled` 完成本次陈旧尝试，让 Controller 从 test 重派；不得发布 pass。否则发布绑定准确 `head_sha` 和当前 card ID 的幂等 v3 `test` 评论。
6. 使用严格 v3 metadata 完成，必含 `protocol_version=hollysys-controller/v1`、卡片 `iteration` 和完整上下文。pass 必须将 `head_sha`、`mr_iid`、`mr_url` 绑定到当前已测试头；scope_gap 必须给目标与问题。必须通过仓库 Schema，不得包含下一卡字段。绝不得解决问题、push、创建卡片/MR 或合并。
