---
name: hollysys-create-tasks
description: 当 SPEC 与 PLAN 已冻结时，为 Kanban 卡生成稳定、无环、可追溯的 TASKS DAG。
version: 2.0.1
---

# 创建 TASKS 集合

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 为检查 worktree、commit 和 push 而明确要求的常规 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是唯一可编辑副本。Controller 已在发卡前对账；不要例行 fetch/pull。只有缺少 ref、已证实头不一致或 push 被拒绝时才能 fetch，并记录原因。
- TASKS 面向既有 MES 仓库执行，不是从零搭建项目。任务必须优先修改或扩展现有
  模块、页面、服务、数据契约和测试；只有 PLAN 以仓库证据证明没有合适落点时，才能
  创建新的承载结构。
- 上游遗漏、歧义或矛盾不得阻塞或返工冻结 SPEC/PLAN。按安全、验收、具体规则、仓库契约/兼容性和最小可逆范围在 TASKS 中形成可执行取舍。
- 只有权限、凭据、环境/能力缺失、自动重试不安全或破坏性动作待授权时才允许人类阻塞。

1. 调用 `kanban_show()`；要求 `created_by=hollysys-controller`，并验证卡片 JSON 的 v3 protocol/mode/run/stage/iteration/assignee/parent、项目、worktree/分支/MR、冻结基线和可选 repair_context。
2. 在 `repository_base_sha` 上核对 PLAN 引用的真实代码、文档和测试路径，确认当前
   行为、复用点与改造范围。对每个匹配的 SPEC/PLAN 键，基于 TASKS 模板编写且只
   编写对应 `task-<key>.md`，阶段内重写保留稳定 ID。
3. 保持严格任务格式并在完整 TASKS 集合中唯一分配 ID。每项任务必须声明对目标
   文件采取 `reuse|modify|extend|create` 中哪种动作；`create` 必须说明现有代码为何
   无法承载。定义需求来源、准确目标文件、依赖、验收和可重复验证，提供无环 DAG、
   执行波次和完整覆盖矩阵。
4. review 重写只修复任务缺陷；冻结违规修复时恢复卡片列出的基线。PLAN/SPEC 不充分时在 TASKS 内记录当前阶段决策，不修改冻结上游。finalization 完成最终取舍且不重编号无关工作。
5. commit 最小且连贯的 TASKS 变更，并 push 到同一分支/MR。绝不得创建 GitLab Issue、Task work item 或 TASKS MR。
6. normal 卡用 v3 pass metadata，绑定共享 `mr_iid`、`mr_url`、当前
   `head_sha`，并提交严格 v7 `repository_evidence`；
   finalization 发布 forced-advance 评论及完整最终基线、决策和风险证据。不得以
   业务缺口 fail，不得实现、审查、创建卡片或合并。
7. 调用完成工具前，必须重新读取当前卡片并逐项原样复制 Controller 上下文：
   `checkout` 取 `run.workspace.checkout`（不是 worktree、终端 cwd 或历史路径），
   `iteration` 取当前卡片 iteration（不是 review 次数）。不得复用父卡或前一次
   metadata。`repository_evidence.inspected_paths` 只允许
   `repository_base_sha` 上已存在的精确路径；逐项执行
   `git cat-file -e "$repository_base_sha:$path"`，不得把本轮新增工件路径列入其中。
- 调用完成工具前，必须将业务 metadata 保存为 JSON，并执行
  `hollysysctl validate-completion --card-id '<当前卡 ID>' --metadata '<json-file>'`；
  只有 Controller 返回 `ok=true` 才能完成卡片。
