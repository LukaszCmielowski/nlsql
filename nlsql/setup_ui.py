"""Small terminal prompts and saved settings for the setup wizard."""

import getpass
import os
from collections.abc import Callable
from pathlib import Path

from dotenv import dotenv_values, set_key

PLACEHOLDERS = {
    "dch.example.com:443",
    "https://model.example.com/v1",
    "postgresql://USERNAME:PASSWORD@HOST:5432/DATABASE",
}


def step(number: int, title: str):
    print(f"\n[{number}/5] {title}\n" + "─" * 56)


def yes(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        answer = input(prompt + suffix).strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes", "n", "no"}:
            return answer in {"y", "yes"}
        print("Enter yes or no.")


def prompt_value(
    label: str,
    *,
    default: str = "",
    secret: bool = False,
    optional: bool = False,
    validate: Callable[[str], str] | None = None,
) -> str:
    """Reprompt locally on invalid input; validators may also normalize values."""
    suffix = " [saved; Enter to keep]" if secret and default else f" [{default}]" if default else ""
    if optional:
        suffix += " (optional; '-' clears)" if default else " (optional; Enter to skip)"
    while True:
        value = (getpass.getpass if secret else input)(label + suffix + ": ").strip()
        value = "" if optional and value == "-" else value or default
        if not value and not optional:
            print("A value is required.")
            continue
        try:
            return validate(value) if value and validate else value
        except ValueError as exc:
            print(str(exc))


def certificate_file(value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_file():
        raise ValueError(
            "Certificate file not found. Enter an existing file path, or '-' to clear."
        )
    return str(path.resolve())


class Settings:
    def __init__(self, path: Path):
        self.path = path
        saved = dotenv_values(path)
        self.values = {**saved, **os.environ}
        self.overrides = sorted(
            key for key, value in saved.items() if key in os.environ and os.environ[key] != value
        )

    def get(self, key: str, default: str = "") -> str:
        value = self.values.get(key) or default
        return "" if value in PLACEHOLDERS else value

    def save(self, key: str, value: str):
        self.path.touch(mode=0o600, exist_ok=True)
        set_key(self.path, key, value)
        self.path.chmod(0o600)
        self.values[key] = value
        os.environ[key] = value

    def prompt(self, key: str, label: str, **options) -> str:
        default = options.pop("default", "")
        value = prompt_value(label, default=self.get(key, default), **options)
        self.save(key, value)
        return value

    def reuse(
        self,
        labels: dict[str, str],
        *,
        required: tuple[str, ...],
        secrets: tuple[str, ...] = (),
        files: tuple[str, ...] = (),
    ) -> bool:
        """Review a complete saved section once instead of re-entering every field."""
        if not all(self.get(key) for key in required):
            return False
        for key in files:
            if self.get(key) and not Path(self.get(key)).expanduser().is_file():
                print(f"Saved {labels[key].lower()} file is missing. Update the settings below.")
                return False
        print("Current settings:")
        for key, label in labels.items():
            value = self.get(key)
            display = "saved (hidden)" if key in secrets and value else value or "(empty)"
            print(f"  {label}: {display}")
        if not yes("Use these settings?", True):
            return False
        for key in files:
            if self.get(key):
                self.save(key, certificate_file(self.get(key)))
        return True
