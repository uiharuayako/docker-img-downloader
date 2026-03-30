from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .compose_support import ComposeImageError, extract_images_from_compose_file, extract_images_from_compose_text, normalize_compose_images
from .config import ServiceConfig, load_service_config
from .image_ref import ImageReferenceError, build_target_image, parse_image_reference, replace_registry


TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
TASK_STATUS_FAILED = "failed"
FINAL_STATES = {TASK_STATUS_SUCCEEDED, TASK_STATUS_FAILED}
MAX_LOG_LINES = 100
BYTE_UNITS = {
    "B": 1,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}
PERCENT_PATTERN = re.compile(r"(?P<percent>\d+(?:\.\d+)?)%")
BYTE_PROGRESS_PATTERN = re.compile(
    r"(?P<completed>\d+(?:\.\d+)?)\s*(?P<completed_unit>[KMGT]?i?B)\s*/\s*"
    r"(?P<total>\d+(?:\.\d+)?)\s*(?P<total_unit>[KMGT]?i?B)",
    re.IGNORECASE,
)
SPEED_PATTERN = re.compile(r"(?P<speed>\d+(?:\.\d+)?)\s*(?P<unit>[KMGT]?i?B/s)", re.IGNORECASE)


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
    phase: str = "queued"
    progress_percent: float | None = None
    bytes_completed: int | None = None
    bytes_total: int | None = None
    speed_bytes_per_sec: float | None = None
    current_source: str | None = None
    local_artifact_path: str | None = None
    logs: list[str] | None = None
    last_updated_at: str | None = None

    def __post_init__(self) -> None:
        if self.logs is None:
            self.logs = []
        if self.last_updated_at is None:
            self.last_updated_at = self.created_at

    def to_dict(self) -> dict[str, Any]:
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
    phase: str
    progress_percent: float | None = None
    bytes_completed: int | None = None
    bytes_total: int | None = None
    speed_bytes_per_sec: float | None = None
    current_source: str | None = None
    local_artifact_path: str | None = None
    logs: list[str]
    last_updated_at: str | None = None


class ComposeSyncRequest(BaseModel):
    compose_yaml: str | None = None
    compose_file_path: str | None = None


class ComposeSyncResponse(BaseModel):
    images: list[str]
    tasks: list[SyncTaskResponse]


class TaskListResponse(BaseModel):
    tasks: list[SyncTaskResponse]


def _to_bytes(value: str, unit: str) -> int:
    multiplier = BYTE_UNITS[unit.upper()]
    return int(float(value) * multiplier)


def _parse_progress_line(line: str) -> dict[str, float | int] | None:
    payload: dict[str, float | int] = {}

    percent_match = PERCENT_PATTERN.search(line)
    if percent_match:
        payload["progress_percent"] = float(percent_match.group("percent"))

    byte_match = BYTE_PROGRESS_PATTERN.search(line)
    if byte_match:
        payload["bytes_completed"] = _to_bytes(byte_match.group("completed"), byte_match.group("completed_unit"))
        payload["bytes_total"] = _to_bytes(byte_match.group("total"), byte_match.group("total_unit"))
        if "progress_percent" not in payload and payload["bytes_total"]:
            payload["progress_percent"] = round(payload["bytes_completed"] / payload["bytes_total"] * 100, 2)

    speed_match = SPEED_PATTERN.search(line)
    if speed_match:
        payload["speed_bytes_per_sec"] = _to_bytes(speed_match.group("speed"), speed_match.group("unit").removesuffix("/s"))

    return payload or None


def _detect_phase(line: str) -> str | None:
    lowered = line.lower()
    if "login" in lowered or "auth" in lowered:
        return "login"
    if "manifest" in lowered:
        return "resolving"
    if "pull" in lowered or "copy" in lowered or "download" in lowered or "fetch" in lowered:
        return "copying"
    if "push" in lowered or "upload" in lowered or "writing" in lowered:
        return "pushing"
    if "export" in lowered or "saving" in lowered:
        return "exporting"
    if "done" in lowered or "complete" in lowered or "success" in lowered:
        return "succeeded"
    return None


def build_artifact_path(cache_dir: str, source_image: str) -> Path:
    parsed = parse_image_reference(source_image)
    safe_repository = re.sub(r"[^a-zA-Z0-9._-]+", "__", parsed.repository)
    safe_tag = re.sub(r"[^a-zA-Z0-9._-]+", "_", parsed.tag)
    safe_registry = re.sub(r"[^a-zA-Z0-9._-]+", "_", parsed.registry)
    filename = f"{safe_registry}__{safe_repository}__{safe_tag}.tar"
    return Path(cache_dir).expanduser().resolve() / filename


def render_dashboard_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Harbor Sync Dashboard</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #0b1020;
      --panel: #131a2a;
      --muted: #8ea0bf;
      --text: #eef4ff;
      --border: #2a344d;
      --accent: #60a5fa;
      --success: #22c55e;
      --warning: #f59e0b;
      --danger: #ef4444;
    }
    body {
      margin: 0;
      font-family: Inter, system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    .layout {
      display: grid;
      grid-template-columns: 360px 1fr;
      gap: 16px;
      min-height: 100vh;
      padding: 16px;
      box-sizing: border-box;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px;
      box-sizing: border-box;
    }
    h1, h2, h3 { margin: 0 0 12px; }
    .muted { color: var(--muted); }
    .stack { display: grid; gap: 12px; }
    label { display: grid; gap: 6px; font-size: 14px; }
    input, textarea, button, select {
      font: inherit;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: #0f1728;
      color: var(--text);
      padding: 10px 12px;
      box-sizing: border-box;
      width: 100%;
    }
    textarea { min-height: 160px; resize: vertical; }
    button {
      background: var(--accent);
      color: #08111f;
      font-weight: 700;
      cursor: pointer;
    }
    button.secondary {
      background: transparent;
      color: var(--text);
    }
    .toolbar {
      display: flex;
      gap: 8px;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 12px;
    }
    .cards {
      display: grid;
      gap: 10px;
      max-height: calc(100vh - 120px);
      overflow: auto;
    }
    .card {
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px;
      cursor: pointer;
      background: #0f1728;
    }
    .card.active { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
    .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .chip {
      font-size: 12px;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid var(--border);
    }
    .queued { color: var(--warning); }
    .running { color: var(--accent); }
    .succeeded { color: var(--success); }
    .failed { color: var(--danger); }
    .progress {
      width: 100%;
      height: 8px;
      background: #0b1020;
      border-radius: 999px;
      overflow: hidden;
      border: 1px solid var(--border);
    }
    .progress > div {
      height: 100%;
      background: linear-gradient(90deg, var(--accent), #34d399);
      width: 0%;
    }
    pre {
      margin: 0;
      padding: 12px;
      border-radius: 12px;
      background: #0a0f1c;
      border: 1px solid var(--border);
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 320px;
    }
    .detail-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    @media (max-width: 1100px) {
      .layout { grid-template-columns: 1fr; }
      .cards { max-height: none; }
      .detail-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="layout">
    <div class="stack">
      <section class="panel stack">
        <div>
          <h1>Harbor Sync Dashboard</h1>
          <div class="muted">查看任务状态、阶段、速度、最近日志，并可直接调试接口。</div>
        </div>
        <form id="sync-form" class="stack">
          <label>单个镜像
            <input id="source-image" name="source_image" placeholder="docker.io/library/nginx:1.27.4" />
          </label>
          <button type="submit">提交同步</button>
        </form>
        <form id="compose-form" class="stack">
          <label>docker-compose YAML
            <textarea id="compose-yaml" name="compose_yaml" placeholder="services:\n  web:\n    image: nginx:1.27.4"></textarea>
          </label>
          <label>上传 YAML 文件
            <input id="compose-file" type="file" accept=".yaml,.yml,text/yaml,text/x-yaml,.txt" />
          </label>
          <div class="muted" id="compose-file-hint">支持上传 `docker-compose.yaml` / `.yml`，读取后会自动填入文本框。</div>
          <button type="submit">按 Compose 同步</button>
        </form>
        <form id="manual-sync-form" class="stack">
          <label>手动发送 `/sync` 请求
            <textarea id="manual-sync-body" name="manual_sync_body" placeholder='{"source_image":"docker.io/library/nginx:1.27.4"}'></textarea>
          </label>
          <div class="muted">这里直接发送 JSON 到 `POST /sync`，便于调试请求体。</div>
          <button type="submit">发送 /sync 请求</button>
        </form>
      </section>
      <section class="panel stack">
        <div class="toolbar">
          <h2>任务列表</h2>
          <button id="refresh-button" type="button" class="secondary">刷新</button>
        </div>
        <div id="task-cards" class="cards"></div>
      </section>
    </div>
    <section class="panel stack">
      <div class="toolbar">
        <h2>任务详情</h2>
        <div id="summary" class="muted">尚未选择任务</div>
      </div>
      <div id="task-detail" class="stack muted">等待任务数据…</div>
    </section>
  </div>
  <script>
    const state = { selectedTaskId: null, tasks: [] };

    function fmtTime(value) {
      if (!value) return "-";
      return new Date(value).toLocaleString();
    }

    function fmtBytes(value) {
      if (value === null || value === undefined) return "-";
      const units = ["B", "KiB", "MiB", "GiB", "TiB"];
      let size = value;
      let idx = 0;
      while (size >= 1024 && idx < units.length - 1) {
        size /= 1024;
        idx += 1;
      }
      return `${size.toFixed(size >= 10 || idx === 0 ? 0 : 1)} ${units[idx]}`;
    }

    function fmtSpeed(value) {
      if (value === null || value === undefined) return "-";
      return `${fmtBytes(value)}/s`;
    }

    function fmtPercent(value) {
      if (value === null || value === undefined) return "未知";
      return `${value.toFixed(1)}%`;
    }

    function escapeHtml(value) {
      return value
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function renderCards() {
      const host = document.getElementById("task-cards");
      if (!state.tasks.length) {
        host.innerHTML = '<div class="muted">暂无任务。</div>';
        return;
      }
      host.innerHTML = state.tasks.map((task) => {
        const active = task.task_id === state.selectedTaskId ? "active" : "";
        const width = Math.max(0, Math.min(100, task.progress_percent ?? 0));
        return `
          <div class="card ${active}" data-task-id="${task.task_id}">
            <div class="row">
              <strong>${escapeHtml(task.source_image)}</strong>
              <span class="chip ${task.status}">${task.status}</span>
              <span class="chip">${task.phase}</span>
            </div>
            <div class="muted">${escapeHtml(task.target_image)}</div>
            <div class="progress" style="margin:10px 0 6px;"><div style="width:${width}%"></div></div>
            <div class="row muted">
              <span>${fmtPercent(task.progress_percent)}</span>
              <span>${fmtSpeed(task.speed_bytes_per_sec)}</span>
              <span>${fmtTime(task.last_updated_at)}</span>
            </div>
          </div>`;
      }).join("");
      for (const el of host.querySelectorAll(".card")) {
        el.addEventListener("click", () => {
          state.selectedTaskId = el.dataset.taskId;
          renderCards();
          renderDetail();
        });
      }
    }

    function renderDetail() {
      const target = document.getElementById("task-detail");
      const task = state.tasks.find((item) => item.task_id === state.selectedTaskId) || state.tasks[0];
      if (!task) {
        target.innerHTML = '<div class="muted">暂无任务。</div>';
        document.getElementById("summary").textContent = "尚未选择任务";
        return;
      }
      state.selectedTaskId = task.task_id;
      document.getElementById("summary").textContent = `${task.status} / ${task.phase}`;
      target.innerHTML = `
        <div class="detail-grid">
          <div>
            <h3>来源</h3>
            <div>${escapeHtml(task.source_image)}</div>
          </div>
          <div>
            <h3>目标</h3>
            <div>${escapeHtml(task.target_image)}</div>
          </div>
          <div>
            <h3>阶段</h3>
            <div>${escapeHtml(task.phase)}</div>
          </div>
          <div>
            <h3>状态</h3>
            <div>${escapeHtml(task.status)}</div>
          </div>
          <div>
            <h3>当前源</h3>
            <div>${escapeHtml(task.current_source || "-")}</div>
          </div>
          <div>
            <h3>本地文件</h3>
            <div>${escapeHtml(task.local_artifact_path || "-")}</div>
          </div>
          <div>
            <h3>消息</h3>
            <div>${escapeHtml(task.message || "-")}</div>
          </div>
          <div>
            <h3>进度</h3>
            <div>${fmtPercent(task.progress_percent)} (${fmtBytes(task.bytes_completed)} / ${fmtBytes(task.bytes_total)})</div>
          </div>
          <div>
            <h3>速度</h3>
            <div>${fmtSpeed(task.speed_bytes_per_sec)}</div>
          </div>
          <div>
            <h3>创建时间</h3>
            <div>${fmtTime(task.created_at)}</div>
          </div>
          <div>
            <h3>开始时间</h3>
            <div>${fmtTime(task.started_at)}</div>
          </div>
          <div>
            <h3>结束时间</h3>
            <div>${fmtTime(task.finished_at)}</div>
          </div>
          <div>
            <h3>最近更新时间</h3>
            <div>${fmtTime(task.last_updated_at)}</div>
          </div>
        </div>
        <div class="stack">
          <h3>最近日志</h3>
          <pre>${escapeHtml((task.logs || []).join("\\n") || "暂无日志")}</pre>
        </div>`;
      renderCards();
    }

    async function refreshTasks() {
      const response = await fetch("/api/tasks");
      const payload = await response.json();
      state.tasks = payload.tasks;
      if (!state.selectedTaskId && state.tasks.length) {
        state.selectedTaskId = state.tasks[0].task_id;
      }
      renderCards();
      renderDetail();
    }

    async function submitJson(url, payload) {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || "请求失败");
      }
      return data;
    }

    function showError(error) {
      const message = error instanceof Error ? error.message : String(error);
      window.alert(message);
    }

    document.getElementById("sync-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const sourceImage = document.getElementById("source-image").value.trim();
        if (!sourceImage) return;
        await submitJson("/sync", { source_image: sourceImage });
        document.getElementById("source-image").value = "";
        await refreshTasks();
      } catch (error) {
        showError(error);
      }
    });

    document.getElementById("compose-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const composeYaml = document.getElementById("compose-yaml").value.trim();
        if (!composeYaml) return;
        await submitJson("/sync/compose", { compose_yaml: composeYaml });
        await refreshTasks();
      } catch (error) {
        showError(error);
      }
    });

    document.getElementById("compose-file").addEventListener("change", async (event) => {
      const [file] = event.target.files || [];
      if (!file) return;
      const text = await file.text();
      document.getElementById("compose-yaml").value = text;
      document.getElementById("compose-file-hint").textContent = `已加载文件：${file.name}`;
    });

    document.getElementById("manual-sync-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const rawBody = document.getElementById("manual-sync-body").value.trim();
        if (!rawBody) return;
        const payload = JSON.parse(rawBody);
        await submitJson("/sync", payload);
        await refreshTasks();
      } catch (error) {
        showError(error);
      }
    });

    document.getElementById("refresh-button").addEventListener("click", refreshTasks);
    refreshTasks();
    setInterval(refreshTasks, 2000);
  </script>
</body>
</html>"""


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
        if self.config.allowed_source_registries and parsed.registry not in self.config.allowed_source_registries:
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

    def list_tasks(self, *, limit: int = 100) -> list[SyncTask]:
        with self._lock:
            tasks = sorted(self._tasks_by_id.values(), key=lambda task: task.created_at, reverse=True)
            return tasks[:limit]

    def _update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        message: str | None = None,
        phase: str | None = None,
        started: bool = False,
        finished: bool = False,
        progress_percent: float | None = None,
        bytes_completed: int | None = None,
        bytes_total: int | None = None,
        speed_bytes_per_sec: float | None = None,
        current_source: str | None = None,
        local_artifact_path: str | None = None,
        append_log: str | None = None,
    ) -> SyncTask:
        with self._lock:
            task = self._tasks_by_id[task_id]
            if status is not None:
                task.status = status
            if message is not None:
                task.message = message
            if phase is not None:
                task.phase = phase
            if started and task.started_at is None:
                task.started_at = utcnow_iso()
            if finished:
                task.finished_at = utcnow_iso()
                self._active_by_source.pop(task.source_image, None)
            if progress_percent is not None:
                task.progress_percent = round(progress_percent, 2)
            if bytes_completed is not None:
                task.bytes_completed = bytes_completed
            if bytes_total is not None:
                task.bytes_total = bytes_total
            if speed_bytes_per_sec is not None:
                task.speed_bytes_per_sec = speed_bytes_per_sec
            if current_source is not None:
                task.current_source = current_source
            if local_artifact_path is not None:
                task.local_artifact_path = local_artifact_path
            if append_log:
                task.logs.append(append_log)
                if len(task.logs) > MAX_LOG_LINES:
                    task.logs = task.logs[-MAX_LOG_LINES:]
            task.last_updated_at = utcnow_iso()
            self._save_tasks()
            return task

    def _run_task(self, task_id: str) -> None:
        task = self._update_task(
            task_id,
            status=TASK_STATUS_RUNNING,
            message="Task started.",
            phase="preparing",
            started=True,
        )
        try:
            with self._copy_lock:
                self._ensure_harbor_login(task_id)
                copied_source = self._copy_image(task_id, task.source_image, task.target_image)
                self._maybe_export_artifact(task_id, copied_source)
            self._update_task(
                task_id,
                status=TASK_STATUS_SUCCEEDED,
                message="Image copied to Harbor.",
                phase="succeeded",
                progress_percent=100.0,
                finished=True,
            )
        except Exception as exc:
            self._update_task(
                task_id,
                status=TASK_STATUS_FAILED,
                message=str(exc),
                phase="failed",
                finished=True,
            )

    def _ensure_harbor_login(self, task_id: str) -> None:
        if not self.config.harbor_username or not self.config.harbor_password:
            raise RuntimeError("Harbor credentials are missing.")

        self._update_task(task_id, phase="login", message="Logging in to Harbor.")
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
        self._run_command(task_id, command, "Harbor login failed", phase="login", sensitive=True)

    def _copy_image(self, task_id: str, source_image: str, target_image: str) -> str:
        parsed = parse_image_reference(source_image)
        mirror_candidates = self.config.registry_mirrors.get(parsed.registry, [])
        source_candidates = [replace_registry(source_image, mirror) for mirror in mirror_candidates]
        source_candidates.append(source_image)

        errors: list[str] = []
        for candidate in source_candidates:
            self._update_task(
                task_id,
                phase="copying",
                message=f"Copying image from {candidate}.",
                current_source=candidate,
                append_log=f"Trying source {candidate}",
            )
            command = [
                self.config.crane_path,
                "copy",
                "--platform",
                self.config.platform,
                candidate,
                target_image,
            ]
            try:
                self._run_command(task_id, command, f"Image copy failed for source {candidate}", phase="copying", current_source=candidate)
                return candidate
            except RuntimeError as exc:
                errors.append(str(exc))

        raise RuntimeError(" | ".join(errors))

    def _maybe_export_artifact(self, task_id: str, source_image: str) -> None:
        if not self.config.keep_downloaded_files:
            return

        artifact_path = build_artifact_path(self.config.download_cache_dir, source_image)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self._update_task(
            task_id,
            phase="exporting",
            message=f"Exporting local image artifact to {artifact_path}.",
            local_artifact_path=str(artifact_path),
            append_log=f"Exporting local artifact to {artifact_path}",
        )
        command = [
            self.config.crane_path,
            "pull",
            "--platform",
            self.config.platform,
            source_image,
            str(artifact_path),
        ]
        self._run_command(
            task_id,
            command,
            f"Artifact export failed for source {source_image}",
            phase="exporting",
            current_source=source_image,
        )

    def _run_command(
        self,
        task_id: str,
        command: list[str],
        failure_prefix: str,
        *,
        phase: str,
        current_source: str | None = None,
        sensitive: bool = False,
    ) -> None:
        environment = os.environ.copy()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
            bufsize=1,
        )

        captured_lines: list[str] = []
        if not sensitive:
            self._update_task(task_id, phase=phase, current_source=current_source, append_log=f"$ {' '.join(command)}")

        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            captured_lines.append(line)
            progress = _parse_progress_line(line)
            next_phase = _detect_phase(line) or phase
            self._update_task(
                task_id,
                phase=next_phase,
                message=line,
                current_source=current_source,
                append_log=line,
                progress_percent=progress["progress_percent"] if progress and "progress_percent" in progress else None,
                bytes_completed=progress["bytes_completed"] if progress and "bytes_completed" in progress else None,
                bytes_total=progress["bytes_total"] if progress and "bytes_total" in progress else None,
                speed_bytes_per_sec=progress["speed_bytes_per_sec"] if progress and "speed_bytes_per_sec" in progress else None,
            )

        process.wait()
        if process.returncode != 0:
            details = " | ".join(captured_lines[-5:]) if captured_lines else "no command output"
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

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(render_dashboard_html())

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/tasks", response_model=TaskListResponse)
    def list_tasks(limit: int = 100) -> TaskListResponse:
        tasks = [serialize_task(task) for task in manager.list_tasks(limit=limit)]
        return TaskListResponse(tasks=tasks)

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

    @app.post("/sync/compose", response_model=ComposeSyncResponse)
    def create_compose_sync(request: ComposeSyncRequest) -> ComposeSyncResponse:
        try:
            if request.compose_yaml:
                images = normalize_compose_images(extract_images_from_compose_text(request.compose_yaml))
            elif request.compose_file_path:
                images = normalize_compose_images(extract_images_from_compose_file(request.compose_file_path))
            else:
                raise HTTPException(status_code=400, detail="compose_yaml or compose_file_path is required.")

            tasks = [serialize_task(manager.submit(image)) for image in images]
            return ComposeSyncResponse(images=images, tasks=tasks)
        except HTTPException:
            raise
        except (ComposeImageError, ImageReferenceError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/tasks/{task_id}", response_model=SyncTaskResponse)
    def get_task(task_id: str) -> SyncTaskResponse:
        task = manager.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found.")
        return serialize_task(task)

    @app.get("/api/tasks/{task_id}", response_model=SyncTaskResponse)
    def get_task_api(task_id: str) -> SyncTaskResponse:
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
