from __future__ import annotations

from app.agents.base_agent import BaseAgent

# Scaffold only. Per the agreed scope, Draft and Summarize are implemented
# first; classification (intent/urgency) is wired into the agent architecture
# but not yet used by the web flow. The `classify` prompt already exists in
# prompts.yaml and app/llm/classify.py holds the parsing logic to build on.


class ClassifyAgent(BaseAgent):
    """Intent/urgency classification agent (not yet implemented)."""

    name = "classify"
    prompt_key = "classify"

    def run(self, *, subject: str, sender: str, body: str):  # noqa: ANN201 - scaffold
        raise NotImplementedError(
            "ClassifyAgent is scaffolded but not implemented yet. "
            "Build on app/llm/classify.py to parse intent/urgency JSON."
        )
