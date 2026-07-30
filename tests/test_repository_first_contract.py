from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class RepositoryFirstContractTests(unittest.TestCase):
    def test_all_authoring_roles_require_repository_evidence(self) -> None:
        skills = (
            "data/profiles/spec-writer/skills/hollysys-write-spec/SKILL.md",
            "data/profiles/planner/skills/hollysys-write-plan/SKILL.md",
            "data/profiles/tasker/skills/hollysys-create-tasks/SKILL.md",
            "data/profiles/coder/skills/hollysys-implement/SKILL.md",
        )
        for relative in skills:
            with self.subTest(skill=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("repository_base_sha", text)
                self.assertIn("repository_evidence", text)
                self.assertTrue("现有" in text or "已有" in text)
                self.assertIn("card-context", text)
                self.assertIn("completion-template", text)
                self.assertIn("validate-completion", text)
                self.assertNotIn("kanban_show()", text)

    def test_reviewers_reject_greenfield_assumptions(self) -> None:
        skills = (
            "data/profiles/spec-reviewer/skills/hollysys-review-spec/SKILL.md",
            "data/profiles/plan-reviewer/skills/hollysys-review-plan/SKILL.md",
            "data/profiles/task-reviewer/skills/hollysys-analyze-tasks/SKILL.md",
            "data/profiles/code-reviewer/skills/hollysys-review-code/SKILL.md",
        )
        for relative in skills:
            with self.subTest(skill=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("repository_base_sha", text)
                self.assertTrue(
                    "绿地" in text
                    or "平行框架" in text
                    or "重复实现已有" in text
                )

    def test_artifact_templates_capture_existing_mes_delta(self) -> None:
        expected = {
            "templates/spec-template.md": "现有 MES 基线与需求增量",
            "templates/plan-template.md": "现有系统盘点与复用方案",
            "templates/tasks-template.md": "仓库落点与变更策略",
        }
        for relative, heading in expected.items():
            with self.subTest(template=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(heading, text)
                self.assertIn("repository_base_sha", text)
        tasks = (ROOT / "templates/tasks-template.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("建立本功能所需的最小项目结构", tasks)
        self.assertIn("reuse|modify|extend|create", tasks)

    def test_feishu_human_messages_use_the_friendly_markdown_contract(
        self,
    ) -> None:
        template = (ROOT / "templates/feishu-messages.md").read_text(
            encoding="utf-8"
        )
        dispatcher = (
            ROOT
            / "data/profiles/dispatcher/skills/"
            "hollysys-dispatch-kanban/SKILL.md"
        ).read_text(encoding="utf-8")
        for text in (template, dispatcher):
            with self.subTest(document=text[:40]):
                self.assertIn("飞书 Markdown", text)
                self.assertIn("**任务 ID：**", text)
                self.assertTrue("Stage" in text or "**阶段：**" in text)
                self.assertIn("Agent", text)
                self.assertIn("原", text)
        self.assertIn("1 + worker_redispatch_limit", template)
        self.assertIn("不回复 `run=... stage=...`", dispatcher)


if __name__ == "__main__":
    unittest.main()
