"""Interactive questionnaire for PRD generation via CLI and slash command."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.prompt import Prompt


class PRDQuestion:
    """A single question in the PRD wizard."""

    def __init__(
        self,
        key: str,
        prompt: str,
        help_text: str = "",
        required: bool = True,
        multiline: bool = False,
    ):
        self.key = key
        self.prompt = prompt
        self.help_text = help_text
        self.required = required
        self.multiline = multiline

    def ask(self, console: Console) -> str:
        """Ask the question and return the answer."""
        if self.help_text:
            console.print(f"[dim]{self.help_text}[/dim]")

        if self.multiline:
            console.print(f"[bold]{self.prompt}[/bold]")
            console.print("[dim]Enter text (Ctrl+D or Ctrl+Z on blank line to finish):[/dim]")
            lines = []
            while True:
                try:
                    line = Prompt.ask("  ")
                    if line:
                        lines.append(line)
                except EOFError:
                    break
            return "\n".join(lines)
        else:
            return Prompt.ask(f"[bold]{self.prompt}[/bold]")


class PRDWizard:
    """Interactive questionnaire for gathering PRD data."""

    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        self.answers: dict[str, Any] = {}

        # Define wizard questions
        self.questions = [
            # Basic Info
            PRDQuestion(
                key="name",
                prompt="PRD Name",
                help_text="What is the name of this PRD? (e.g., 'KellerAI Design System')",
                required=True,
            ),
            PRDQuestion(
                key="idea",
                prompt="Core Idea / Problem Statement",
                help_text="Describe the core idea, problem, or concept you're addressing.",
                required=True,
                multiline=True,
            ),
            # Scope & Constraints
            PRDQuestion(
                key="scope_in",
                prompt="What is IN SCOPE?",
                help_text="List the key areas and features explicitly included in this PRD.",
                required=False,
                multiline=True,
            ),
            PRDQuestion(
                key="scope_out",
                prompt="What is OUT OF SCOPE?",
                help_text="List the areas explicitly excluded or deferred for future work.",
                required=False,
                multiline=True,
            ),
            PRDQuestion(
                key="personas",
                prompt="Target Personas / Users",
                help_text="Who are the primary users or personas? What are their key needs?",
                required=False,
                multiline=True,
            ),
            PRDQuestion(
                key="success_metrics",
                prompt="Success Metrics",
                help_text="How will you measure success? What are the key success criteria?",
                required=False,
                multiline=True,
            ),
            # Round 1: Design Tokens
            PRDQuestion(
                key="round1_focus",
                prompt="Round 1 Focus: Foundational Tokens & Structure",
                help_text="What foundational elements need definition? (e.g., colors, spacing, typography, shadows, etc.)",
                required=False,
                multiline=True,
            ),
            # Round 2: Components
            PRDQuestion(
                key="round2_focus",
                prompt="Round 2 Focus: Core Components",
                help_text="What 3-5 core components are essential? What are their roles?",
                required=False,
                multiline=True,
            ),
            # Round 3: Layouts & Responsiveness
            PRDQuestion(
                key="round3_focus",
                prompt="Round 3 Focus: Layouts & Responsive Design",
                help_text="What responsive breakpoints and layout templates matter? Mobile-first approach?",
                required=False,
                multiline=True,
            ),
        ]

    def run(self) -> dict[str, Any]:
        """Run the full wizard questionnaire and return collected answers."""
        self.console.print("\n[bold cyan]🚀 PRD Generation Wizard[/bold cyan]\n")
        self.console.print(
            "[dim]Answer the questions below to generate your PRD. "
            "Press Enter to skip optional questions.[/dim]\n"
        )

        for i, question in enumerate(self.questions, 1):
            self.console.print(f"[bold]Step {i}/{len(self.questions)}[/bold]")
            try:
                answer = question.ask(self.console)
                if answer or not question.required:
                    self.answers[question.key] = answer
                    self.console.print()
                else:
                    self.console.print("[red]✗ This question is required.[/red]\n")
                    # Re-ask if required and empty
                    i -= 1
            except KeyboardInterrupt:
                self.console.print("\n[yellow]⚠ Wizard cancelled.[/yellow]")
                raise

        self.console.print("[bold green]✓ Wizard complete![/bold green]\n")
        return self.answers

    def structure_answers(self) -> tuple[dict[str, str], dict[str, Any]]:
        """Structure raw wizard answers into PRD fields and round-specific JSON.

        Returns:
            (basic_fields, round_answers)
            - basic_fields: {"name", "idea", "scope_in", "scope_out", "personas", "success_metrics"}
            - round_answers: {"round1_tokens", "round2_components", "round3_layouts"}
        """
        basic_fields = {
            "name": self.answers.get("name", ""),
            "idea": self.answers.get("idea", ""),
            "scope_in": self.answers.get("scope_in", ""),
            "scope_out": self.answers.get("scope_out", ""),
            "personas": self.answers.get("personas", ""),
            "success_metrics": self.answers.get("success_metrics", ""),
        }

        round_answers = {
            "round1_tokens": {
                "focus": self.answers.get("round1_focus", ""),
            },
            "round2_components": {
                "focus": self.answers.get("round2_focus", ""),
            },
            "round3_layouts": {
                "focus": self.answers.get("round3_focus", ""),
            },
        }

        return basic_fields, round_answers
