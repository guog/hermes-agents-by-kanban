---
name: hollysys-write-plan
description: 当 SPEC 已冻结且 Kanban 卡要求规划时，在唯一共享 MR 编写可验证的完整 PLAN 集。
version: 2.0.1
---

# 编写 PLAN 集合

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 为检查 worktree、commit 和 push 而明确要求的常规 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是唯一可编辑副本。Controller 已在发卡前对账；不要例行 fetch/pull。只有缺少 ref、已证实头不一致或 push 被拒绝时才能 fetch，并记录原因。
- 本项目是既有 MES 产品定制。PLAN 必须落到 `repository_base_sha` 上真实存在的
  模块、接口、数据结构、配置、测试和扩展模式，优先复用或局部改造；不得默认新增
  一套平行架构、服务、页面框架或数据模型。
- PRD/SPEC 遗漏、歧义或矛盾不得阻塞或触发上游返工。依次按安全/数据完整性、明确验收、具体规则、仓库契约与兼容性、最小可逆方案形成 PLAN 决策并记录未采用方案和回滚方式。
- 只有权限、凭据、环境/能力缺失、自动重试不安全或破坏性动作待授权时才允许人类阻塞；Controller outbox 负责原渠道通知，不管理订阅。

1. 调用 `kanban_show()`；要求 `created_by=hollysys-controller`，并验证卡片 JSON 的 v3 protocol/mode/run/stage/iteration/assignee/parent、项目、worktree/分支、PRD、交付 MR、冻结基线和可选 repair_context。
2. 在 `repository_base_sha` 上阅读仓库规则、架构、相关实现、配置、迁移和测试，形成
   精确的现有系统盘点：哪些能力直接复用、哪些局部修改、哪些扩展点承载新增能力，
   以及影响和兼容边界。对于每个 `spec-<key>.md`，基于 PLAN 模板创建且只创建对应
   `plan-<key>.md`；不得重命名已有文档。
3. 保留模板中必需的技术上下文、治理检查、决策、架构、接口、数据/迁移、兼容性、可观测性、安全性、测试、回滚、真实项目结构、可追溯性和风险章节。将决策追溯到 SPEC 需求，但不得改变意图或定义最终 Task ID。
4. review 重写只修改受影响 PLAN 并保留键；冻结违规修复时按 `frozen_baselines` 恢复 PRD/SPEC。不得修改冻结上游。`mode=finalization` 时完成最后取舍并记录残余风险。
5. commit 最小且连贯的 PLAN 变更，并 push 到同一分支/MR。绝不得创建 PLAN MR。
6. 普通/重写卡以 v3 `mode=normal,outcome=pass` 完成，并提交严格 v7
   `repository_evidence`。不得因业务缺口 fail。finalization 按 forced-advance
   模板发布决策评论并提交完整最终基线、决策和风险证据后 pass。不得创建下一卡、
   审查或合并。
7. 调用完成工具前，必须重新读取当前卡片并逐项原样复制 Controller 上下文：
   `checkout` 取 `run.workspace.checkout`（不是 worktree、终端 cwd 或历史路径），
   `iteration` 取当前卡片 iteration（不是 review 次数）。不得复用父卡或前一次
   metadata。`repository_evidence.inspected_paths` 只允许
   `repository_base_sha` 上已存在的精确路径；逐项执行
   `git cat-file -e "$repository_base_sha:$path"`，不得把本轮新增工件路径列入其中。
- 调用完成工具前，必须将业务 metadata 保存为 JSON，并执行
  `hollysysctl validate-completion --card-id '<当前卡 ID>' --metadata '<json-file>'`；
  只有 Controller 返回 `ok=true` 才能完成卡片。
