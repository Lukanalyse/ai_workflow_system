from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class PromptLoader:
    def __init__(self, prompt_file: Path) -> None:
        self.prompt_file = prompt_file
        self._cache: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        if self._cache is None:
            with self.prompt_file.open("r", encoding="utf-8") as handle:
                self._cache = yaml.safe_load(handle) or {}
        return self._cache

    def get(self, key: str) -> dict[str, str]:
        prompts = self.load()
        value = prompts.get(key)
        if not isinstance(value, dict) or "system" not in value or "user_template" not in value:
            raise KeyError(f"Prompt section '{key}' missing or invalid")
        return {"system": str(value["system"]), "user_template": str(value["user_template"])}

