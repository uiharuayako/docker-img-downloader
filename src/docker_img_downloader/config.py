from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def expand_env_vars(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: expand_env_vars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    if isinstance(value, str):
        return ENV_PATTERN.sub(lambda match: os.environ.get(match.group(1), match.group(0)), value)
    return value


@dataclass(slots=True)
class ServiceConfig:
    harbor_registry: str
    harbor_project: str
    harbor_username: str | None = None
    harbor_password: str | None = None
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    platform: str = "linux/amd64"
    allowed_source_registries: list[str] = field(default_factory=lambda: ["docker.io", "ghcr.io"])
    registry_mirrors: dict[str, list[str]] = field(default_factory=dict)
    crane_path: str = "crane.exe"
    harbor_scheme: str = "https"
    verify_tls: bool = True
    task_store_path: str = "data/tasks.json"
    request_timeout_seconds: int = 30
    keep_downloaded_files: bool = False
    download_cache_dir: str = "data/cache"


def load_yaml(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    load_dotenv(find_dotenv_for_config(config_path), override=False)
    with config_path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {path}, got {type(data).__name__}")
    return expand_env_vars(data)


def find_dotenv_for_config(config_path: Path) -> str | None:
    candidates = [
        config_path.parent / ".env",
        config_path.parent.parent / ".env",
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return str(candidate)
    return None


def load_service_config(path: str | Path) -> ServiceConfig:
    raw = load_yaml(path)
    return ServiceConfig(**raw)
