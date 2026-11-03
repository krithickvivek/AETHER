"""Developer Agent: turns an approved plan into project files from tested templates.

It never asks a language model to write code. It picks a supported template, writes the
files through the run's Workspace (outside AETHER's own folder), and records each file's
checksum and the plan steps it carries out. It does not run any generated code.
"""

from dataclasses import dataclass

from aether.agents import Agent, AgentResult
from aether.context import DONE, FAILED, ProjectContext, RunStore
from aether.templates import TEMPLATES, ProjectTemplate, TemplateChoice, choose_template, map_coverage
from aether.workspace import Workspace, WorkspaceError


@dataclass
class DevelopmentPreview:
    """What the Developer would generate, shown at Checkpoint A before anything is written."""

    choice: TemplateChoice
    files: list[str]
    covers: dict[str, list[int]]
    uncovered_steps: list[int]

    @property
    def template(self) -> ProjectTemplate | None:
        return self.choice.template

    def summary(self) -> str:
        if self.template is None:
            return self.choice.reason
        text = f"{self.template.title}: {len(self.files)} files planned"
        if self.choice.unsupported:
            text += f"; not covered: {', '.join(self.choice.unsupported)}"
        return text


def preview(context: ProjectContext) -> DevelopmentPreview:
    choice = choose_template(context.brief)
    if choice.template is None or context.plan is None:
        return DevelopmentPreview(choice, [], {}, [])
    covers, uncovered = map_coverage(context.plan, choice.template)
    return DevelopmentPreview(choice, [f.path for f in choice.template.files], covers, uncovered)


class DeveloperAgent(Agent):
    name = "Developer"
    action = "Generate project files"
    checkpoint = "A"
    checkpoint_title = "Approve the plan before files are generated"

    def __init__(self, store: RunStore | None = None):
        self.store = store or RunStore()

    def preview(self, context: ProjectContext) -> str:
        return preview(context).summary()

    def run(self, context: ProjectContext) -> AgentResult:
        if context.brief is None or context.plan is None:
            return AgentResult(FAILED, "There is no approved plan to build from.")
        plan_preview = preview(context)
        template = plan_preview.template
        if template is None:
            supported = ", ".join(t.title for t in TEMPLATES)
            return AgentResult(FAILED, f"Unsupported requirement: no template fits it. Supported: {supported}.",
                               notes={"developer_unsupported": plan_preview.choice.reason})

        workspace = Workspace(self.store.workspace_dir(context.run_id))
        records = []
        try:
            for path, text in template.render(context.brief).items():
                record = workspace.write_text(path, text, author=self.name)
                record.covers = plan_preview.covers.get(path, [])
                records.append(record)
        except (WorkspaceError, OSError) as error:
            return AgentResult(FAILED, f"Could not write project files: {error}",
                               updates={"files": records, "project_type": template.key})

        notes = {"developer_template": template.title}
        detail = f"{len(records)} files for the {template.title} ({sum(r.size for r in records):,} bytes)"
        if plan_preview.choice.unsupported:
            notes["developer_unsupported"] = ", ".join(plan_preview.choice.unsupported)
            detail += f"; not covered: {notes['developer_unsupported']}"
        if plan_preview.uncovered_steps:
            notes["developer_uncovered_steps"] = ", ".join(map(str, plan_preview.uncovered_steps))
            detail += f"; plan steps without a file: {notes['developer_uncovered_steps']}"
        return AgentResult(DONE, detail, updates={"files": records, "project_type": template.key}, notes=notes)
