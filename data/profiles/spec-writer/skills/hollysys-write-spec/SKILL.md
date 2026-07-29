---
name: hollysys-write-spec
description: 当普通、重写或 finalization 卡要求编写 SPEC 时，在唯一共享 MR 生成可测试的 SPEC 集并遵守冻结基线。
version: 2.0.1
---

# 编写 SPEC 集合

## 全体 Agent 执行规则

- GitLab 项目、仓库元数据、MR、流水线、讨论和评论操作只能通过锁定版本的 `glab` CLI 或已安装的官方 `glab` Skill 完成。不得改用原始 HTTP/`curl`、临时 SDK、浏览器或人工 UI 操作。本 Skill 为检查 worktree、commit 和 push 而明确要求的常规 `git` 命令仍可使用。
- 卡片指定的 Hermes 共享 `worktree` 是唯一可编辑副本。Controller 已在发卡前对账；不要例行 fetch/pull。只有缺少 ref、已证实本地/远端头不一致或 push 被拒绝时才能 fetch，并记录原因。
- 本交付是对既有企业 MES 产品的客户定制，不是绿地项目。必须从卡片的
  `repository_base_sha` 开始读取已有代码、业务文档、架构、配置、测试和仓库约定，
  先识别可复用能力与当前行为，再把 PRD 表达为对现有功能的扩展、修改或两者组合。
  不得仅凭 PRD 从零设计一套平行系统。
- PRD 遗漏、歧义或自相矛盾不得阻塞。依次按安全/数据完整性、明确验收、具体规则优先于一般描述、仓库契约与兼容性、最小可逆范围作出决定，并写入 SPEC 与 MR 的关键决策；保留未采用方案、影响和回退方式。
- 只有权限、凭据、环境/能力缺失、自动重试不再安全或破坏性动作待授权时才执行“人类阻塞协议”：写幂等 `[human-block:v1]` 评论后 `kanban_block`。Controller outbox 负责原渠道通知；不得自行发飞书、管理订阅、unblock 或创建恢复卡。

1. 调用 `kanban_show()` 并要求 `created_by=hollysys-controller`，且卡片 JSON 的 v3 protocol/mode/run/stage/iteration/assignee/parent、冻结基线和可选 repair_context 与当前卡一致；对照 GitLab 验证项目、worktree/分支、PRD blob 和运行键。绝不得发现或克隆另一个仓库。
2. 读取准确 commit 上的 PRD，并在 `repository_base_sha` 上盘点相关现有能力。至少检查
   仓库规则/架构文档、相邻业务模块、数据或接口契约、已有测试；记录精确路径、当前
   可观察行为、可复用点和 PRD 要求的差异。若没有同类业务能力，也必须识别将承载
   新功能的现有框架、扩展点和约定。
3. 只按可以独立理解和测试的业务范围拆分；定义有序的键、依赖关系和完整的 PRD
   覆盖矩阵。基于中文模板 `/opt/fleet/templates/spec-template.md` 编写每个
   `docs/prds/<prd-basename>/specs/spec-<key>.md`，明确“现状 → PRD 变化 → 期望结果”，
   但不泄漏技术实现选择。键必须稳定，替换全部占位符。
4. `repair_context.kind=review_failure` 时逐项处理 findings，保留稳定键；`frozen_artifact_violation` 时只按卡片 baseline 恢复冻结文件，再在 SPEC 内吸收当前阶段适配。不得修改 PRD。
5. 按仓库的约定式提交规则 commit 最小且连贯的 SPEC 变更，并 push 到共享分支。首次有效 SPEC commit 后，先对账，再填充 MR 描述（包括 `## 关键自主决策`；没有时填写 `无`），然后使用 `/opt/fleet/templates/mr-description.md` 创建且只创建一个 `Draft: [PRD] <prd-basename>.md` MR；此后只更新该 MR 及其决策章节。
6. 普通/重写卡以 `mode=normal,outcome=pass` 提交严格 v7 metadata，绑定共享
   `mr_iid`、`mr_url`、当前 `head_sha`，并以 `repository_evidence`
   绑定仓库基线、检查路径、现有能力、变更类型和复用决策；
   不得用业务问题返回 fail。若 `mode=finalization`，尽量修复第三次 findings，在
   SPEC 中记录最终取舍，并按 `/opt/fleet/templates/forced-advance-comment.md`
   发布唯一幂等评论；计算最终工件证据并填写完整 `forced_advance` 后 pass。
   不得创建下一卡、审查或合并。
7. 调用完成工具前，必须重新读取当前卡片并逐项原样复制 Controller 上下文：
   `checkout` 取 `run.workspace.checkout`（不是 worktree、终端 cwd 或历史路径），
   `iteration` 取当前卡片 iteration（不是 review 次数）。不得复用父卡或前一次
   metadata。`repository_evidence.inspected_paths` 只允许
   `repository_base_sha` 上已存在的精确路径；逐项执行
   `git cat-file -e "$repository_base_sha:$path"`，不得把本轮新增工件路径列入其中。
- 调用完成工具前，必须将业务 metadata 保存为 JSON，并执行
  `hollysysctl validate-completion --card-id '<当前卡 ID>' --metadata '<json-file>'`；
  只有 Controller 返回 `ok=true` 才能完成卡片。
