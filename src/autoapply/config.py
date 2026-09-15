"""Load profile.yaml, settings.yaml and .env."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"

FILL_IN_MARKER = "[FILL IN"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    with path.open() as f:
        return yaml.safe_load(f) or {}


def resolve_path(p: str | Path, base: Path = PROJECT_ROOT) -> Path:
    path = Path(p).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and FILL_IN_MARKER in value


@dataclass
class Profile:
    data: dict[str, Any]

    def get(self, dotted: str, default: Any = "") -> Any:
        """Return a value by dotted path; placeholders ([FILL IN]) return default."""
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        if is_placeholder(cur) or cur is None:
            return default
        return cur

    def has(self, dotted: str) -> bool:
        return self.get(dotted, None) not in (None, "")

    def placeholders(self) -> list[str]:
        """Dotted paths whose values are still [FILL IN]."""
        out: list[str] = []

        def walk(node: Any, prefix: str) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{prefix}.{k}" if prefix else k)
            elif is_placeholder(node):
                out.append(prefix)

        walk(self.data, "")
        return out

    @property
    def resume_pdf(self) -> Path:
        return resolve_path(self.get("resume.pdf", "./resume/Divyarith_Resume.pdf"))

    @property
    def resume_text_path(self) -> Path:
        return resolve_path(self.get("resume.text", "./resume/resume.txt"))

    def resume_text(self) -> str:
        p = self.resume_text_path
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def summary_for_llm(self) -> str:
        """Profile as a compact text block for LLM context (no placeholders)."""
        lines: list[str] = []

        def walk(node: Any, prefix: str) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{prefix}.{k}" if prefix else k)
            elif node not in (None, "") and not is_placeholder(node):
                lines.append(f"{prefix}: {node}")

        walk({k: v for k, v in self.data.items() if k != "resume"}, "")
        return "\n".join(lines)


@dataclass
class Settings:
    data: dict[str, Any]

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return default if cur is None else cur

    # convenience accessors
    @property
    def database_path(self) -> Path:
        return resolve_path(self.get("run.database", "./data/applications.db"))

    @property
    def screenshots_dir(self) -> Path:
        return resolve_path(self.get("run.screenshots_dir", "./data/screenshots"))

    @property
    def logs_dir(self) -> Path:
        return resolve_path(self.get("run.logs_dir", "./logs"))

    @property
    def browser_profile_dir(self) -> Path:
        return resolve_path(self.get("run.browser_profile_dir", "./data/browser_profile"))

    @property
    def listings_repo_path(self) -> Path:
        return resolve_path(self.get("listings.local_path", "./data/listings_repo"))


@dataclass
class Secrets:
    anthropic_api_key: str = ""
    workday_accounts: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Secrets":
        raw = os.environ.get("WORKDAY_ACCOUNTS", "").strip()
        accounts: dict[str, dict[str, str]] = {}
        if raw:
            try:
                accounts = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"WORKDAY_ACCOUNTS in .env is not valid JSON: {e}") from e
        return cls(
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
            workday_accounts=accounts,
        )


def load_profile(path: Path | None = None) -> Profile:
    return Profile(_read_yaml(path or CONFIG_DIR / "profile.yaml"))


def load_settings(path: Path | None = None) -> Settings:
    return Settings(_read_yaml(path or CONFIG_DIR / "settings.yaml"))


def load_secrets(env_path: Path | None = None) -> Secrets:
    load_dotenv(env_path or PROJECT_ROOT / ".env")
    return Secrets.from_env()
