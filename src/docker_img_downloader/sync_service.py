from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .config import ServiceConfig, load_service_config
from .image_ref import ImageReferenceError, build_target_image, parse_image_reference, replace_registry


TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
TASK_STATUS_FAILED = "failed"
FINAL_STATES = {TASK_STATUS_SUCCEEDED, TASK_STATUS_FAILED}


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class SyncTask:
    task_id: str
    source_image: str
    target_image: str
    status: str
    message: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


class SyncRequest(BaseModel):
    source_image: str


class SyncTaskResponse(BaseModel):
    task_id: str
    status: str
    source_image: str
    target_image: str
    message: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None


class TaskManager:
    def __init__(self, config: ServiceConfig):
        self.config = config
        self._lock = threading.Lock()
        self._copy_lock = threading.Lock()
        self._tasks_by_id: dict[str, SyncTask] = {}
        self._active_by_source: dict[str, str] = {}
        self._task_store_path = Path(config.task_store_path).expanduser().resolve()
        self._load_tasks()

    def _load_tasks(self) -> None:
        if not self._task_store_path.exists():
            return
        with self._task_store_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        for item in payload.get("tasks", []):
            task = SyncTask(**item)
            if task.status not in FINAL_STATES:
                task.status = TASK_STATUS_FAILED
                task.message = "Task service restarted before completion."
                task.finished_at = utcnow_iso()
            self._tasks_by_id[task.task_id] = task

    def _save_tasks(self) -> None:
        self._task_store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tasks": [task.to_dict() for task in self._tasks_by_id.values()]}
        with self._task_store_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, ensure_ascii=False)

    def submit(self, source_image: str) -> SyncTask:
        parsed = parse_image_reference(source_image)
        if parsed.registry not in self.config.allowed_source_registries:
            allowed = ", ".join(self.config.allowed_source_registries)
            raise ValueError(f"Registry '{parsed.registry}' is not allowed. Allowed: {allowed}")

        target_image = build_target_image(
            source_image=source_image,
            harbor_registry=self.config.harbor_registry,
            harbor_project=self.config.harbor_project,
        )

        with self._lock:
            active_task_id = self._active_by_source.get(source_image)
            if active_task_id:
                return self._tasks_by_id[active_task_id]

            task = SyncTask(
                task_id=uuid.uuid4().hex,
                source_image=source_image,
                target_image=target_image,
                status=TASK_STATUS_QUEUED,
                message="Task queued.",
                created_at=utcnow_iso(),
            )
            self._tasks_by_id[task.task_id] = task
            self._active_by_source[source_image] = task.task_id
            self._save_tasks()

        thread = threading.Thread(target=self._run_task, args=(task.task_id,), daemon=True)
        thread.start()
        return task

    def get(self, task_id: str) -> SyncTask | None:
        with self._lock:
            return self._tasks_by_id.get(task_id)

    def _set_status(self, task_id: str, status: str, message: str, *, started: bool = False, finished: bool = False) -> SyncTask:
        with self._lock:
            task = self._tasks_by_id[task_id]
            task.status = status
            task.message = message
            if started and task.started_at is None:
                task.started_at = utcnow_iso()
            if finished:
                task.finished_at = utcnow_iso()
                self._active_by_source.pop(task.source_image, None)
            self._save_tasks()
            return task

    def _run_task(self, task_id: str) -> None:
        task = self._set_status(task_id, TASK_STATUS_RUNNING, "Task started.", started=True)
        try:
            with self._copy_lock:
                self._ensure_harbor_login()
                self._copy_image(task.source_image, task.target_image)
            self._set_status(task_id, TASK_STATUS_SUCCEEDED, "Image copied to Harbor.", finished=True)
        except Exception as exc:
            self._set_status(task_id, TASK_STATUS_FAILED, str(exc), finished=True)

    def _ensure_harbor_login(self) -> None:
        if not self.config.harbor_username or not self.config.harbor_password:
            raise RuntimeError("Harbor credentials are missing.")

        command = [
            self.config.crane_path,
            "auth",
            "login",
            self.config.harbor_registry,
            "-u",
            self.config.harbor_username,
            "-p",
            self.config.harbor_password,
        ]
        self._run_command(command, "Harbor login failed")

    def _copy_image(self, source_image: str, target_image: str) -> None:
        parsed = parse_image_reference(source_image)
        mirror_candidates = self.config.registry_mirrors.get(parsed.registry, [])
        source_candidates = [replace_registry(source_image, mirror) for mirror in mirror_candidates]
        source_candidates.append(source_image)

        errors: list[str] = []
        for candidate in source_candidates:
            command = [
                self.config.crane_path,
                "copy",
                "--platform",
                self.config.platform,
                candidate,
                target_image,
            ]
            try:
                self._run_command(command, f"Image copy failed for source {candidate}")
                return
            except RuntimeError as exc:
                errors.append(str(exc))

        raise RuntimeError(" | ".join(errors))

    def _run_command(self, command: list[str], failure_prefix: str) -> None:
        environment = os.environ.copy()
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            stdout = completed.stdout.strip()
            details = stderr or stdout or "no command output"
            raise RuntimeError(f"{failure_prefix}: {details}")


def serialize_task(task: SyncTask) -> SyncTaskResponse:
    return SyncTaskResponse(**task.to_dict())


def create_app() -> FastAPI:
    config_path = os.environ.get("DOCKER_IMG_DOWNLOADER_CONFIG", "config/service.yaml")
    config = load_service_config(config_path)
    manager = TaskManager(config)

    app = FastAPI(title="Harbor Sync Service", version="0.1.0")
    app.state.config = config
    app.state.manager = manager

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/sync", response_model=SyncTaskResponse)
    def create_sync_task(request: SyncRequest) -> SyncTaskResponse:
        try:
            task = manager.submit(request.source_image)
        except ImageReferenceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return serialize_task(task)

    @app.get("/tasks/{task_id}", response_model=SyncTaskResponse)
    def get_task(task_id: str) -> SyncTaskResponse:
        task = manager.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found.")
        return serialize_task(task)

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Windows Harbor sync service.")
    parser.add_argument("--config", default="config/service.yaml", help="Path to the YAML config file.")
    args = parser.parse_args()

    config = load_service_config(args.config)
    os.environ["DOCKER_IMG_DOWNLOADER_CONFIG"] = str(Path(args.config).expanduser().resolve())
    uvicorn.run(
        "docker_img_downloader.sync_service:create_app",
        factory=True,
        host=config.listen_host,
        port=config.listen_port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
