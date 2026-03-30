from fastapi.testclient import TestClient

from docker_img_downloader.sync_service import create_app


def write_config(
    tmp_path,
    extra_lines: list[str] | None = None,
    *,
    include_allowed_source_registries: bool = True,
):
    config_path = tmp_path / "service.yaml"
    lines = [
        "harbor_registry: harbor.intra.local",
        "harbor_project: mirror",
        "harbor_username: user@example.com",
        "harbor_password: pass",
        "listen_host: 127.0.0.1",
        "listen_port: 8080",
        "platform: linux/amd64",
        "crane_path: crane.exe",
        f"task_store_path: {tmp_path / 'tasks.json'}",
    ]
    if include_allowed_source_registries:
        lines.extend(
            [
                "allowed_source_registries:",
                "  - docker.io",
                "  - ghcr.io",
            ]
        )
    if extra_lines:
        lines.extend(extra_lines)
    config_path.write_text("\n".join(lines), encoding="utf-8")
    return config_path


def test_healthz(monkeypatch, tmp_path) -> None:
    config_path = write_config(
        tmp_path,
        extra_lines=[
            "registry_mirrors:",
            "  docker.io:",
            "    - docker.m.daocloud.io",
        ],
    )
    monkeypatch.setenv("DOCKER_IMG_DOWNLOADER_CONFIG", str(config_path))

    app = create_app()
    client = TestClient(app)

    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_sync_reuses_active_task_with_mirror_config(monkeypatch, tmp_path) -> None:
    config_path = write_config(
        tmp_path,
        extra_lines=[
            "registry_mirrors:",
            "  docker.io:",
            "    - docker.m.daocloud.io",
        ],
    )
    monkeypatch.setenv("DOCKER_IMG_DOWNLOADER_CONFIG", str(config_path))

    app = create_app()
    manager = app.state.manager

    def slow_run(task_id, *args, **kwargs):
        import time

        manager._update_task(task_id, append_log="copy in progress", phase="copying")
        time.sleep(0.2)
        return None

    monkeypatch.setattr(manager, "_run_command", slow_run)
    client = TestClient(app)

    first = client.post("/sync", json={"source_image": "docker.io/library/nginx:1.27.4"})
    second = client.post("/sync", json={"source_image": "docker.io/library/nginx:1.27.4"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["task_id"] == second.json()["task_id"]


def test_sync_compose_accepts_yaml(monkeypatch, tmp_path) -> None:
    config_path = write_config(tmp_path)
    monkeypatch.setenv("DOCKER_IMG_DOWNLOADER_CONFIG", str(config_path))

    app = create_app()
    manager = app.state.manager
    monkeypatch.setattr(manager, "_run_command", lambda *args, **kwargs: None)
    client = TestClient(app)

    compose_yaml = """
services:
  web:
    image: nginx:1.27.4
  api:
    image: ghcr.io/example/app:v1
"""
    response = client.post("/sync/compose", json={"compose_yaml": compose_yaml})
    assert response.status_code == 200
    payload = response.json()
    assert payload["images"] == [
        "docker.io/library/nginx:1.27.4",
        "ghcr.io/example/app:v1",
    ]
    assert len(payload["tasks"]) == 2


def test_sync_allows_any_registry_when_allowlist_is_not_configured(monkeypatch, tmp_path) -> None:
    config_path = write_config(tmp_path, include_allowed_source_registries=False)
    monkeypatch.setenv("DOCKER_IMG_DOWNLOADER_CONFIG", str(config_path))

    app = create_app()
    manager = app.state.manager
    monkeypatch.setattr(manager, "_run_command", lambda *args, **kwargs: None)
    client = TestClient(app)

    response = client.post("/sync", json={"source_image": "quay.io/prometheus/node-exporter:v1.8.2"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_image"] == "quay.io/prometheus/node-exporter:v1.8.2"


def test_loads_env_file_for_config(monkeypatch, tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "HARBOR_USERNAME=env-user@example.com",
                "HARBOR_PASSWORD=env-password",
            ]
        ),
        encoding="utf-8",
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "service.yaml"
    config_path.write_text(
        "\n".join(
            [
                "harbor_registry: harbor.intra.local",
                "harbor_project: mirror",
                "harbor_username: ${HARBOR_USERNAME}",
                "harbor_password: ${HARBOR_PASSWORD}",
                "listen_host: 127.0.0.1",
                "listen_port: 8080",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("HARBOR_USERNAME", raising=False)
    monkeypatch.delenv("HARBOR_PASSWORD", raising=False)
    monkeypatch.setenv("DOCKER_IMG_DOWNLOADER_CONFIG", str(config_path))

    app = create_app()

    assert app.state.config.harbor_username == "env-user@example.com"
    assert app.state.config.harbor_password == "env-password"


def test_dashboard_page_is_served(monkeypatch, tmp_path) -> None:
    config_path = write_config(tmp_path)
    monkeypatch.setenv("DOCKER_IMG_DOWNLOADER_CONFIG", str(config_path))

    app = create_app()
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "Harbor Sync Dashboard" in response.text
    assert "任务列表" in response.text
    assert "上传 YAML 文件" in response.text
    assert "手动发送 `/sync` 请求" in response.text


def test_api_lists_tasks_with_progress_fields(monkeypatch, tmp_path) -> None:
    config_path = write_config(tmp_path)
    monkeypatch.setenv("DOCKER_IMG_DOWNLOADER_CONFIG", str(config_path))

    app = create_app()
    manager = app.state.manager

    def fake_run(task_id, command, failure_prefix, **kwargs):
        if "auth" in command:
            manager._update_task(task_id, phase="login", append_log="login ok")
            return None
        manager._update_task(
            task_id,
            phase="copying",
            message="copying",
            append_log="12 MiB / 24 MiB 50% 4 MiB/s",
            progress_percent=50.0,
            bytes_completed=12 * 1024 * 1024,
            bytes_total=24 * 1024 * 1024,
            speed_bytes_per_sec=4 * 1024 * 1024,
            current_source="docker.io/library/nginx:1.27.4",
        )
        return None

    monkeypatch.setattr(manager, "_run_command", fake_run)
    client = TestClient(app)

    submit_response = client.post("/sync", json={"source_image": "docker.io/library/nginx:1.27.4"})
    assert submit_response.status_code == 200
    task_id = submit_response.json()["task_id"]

    detail_response = client.get(f"/api/tasks/{task_id}")
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["phase"] in {"copying", "succeeded"}
    assert "logs" in detail_payload

    list_response = client.get("/api/tasks")
    assert list_response.status_code == 200
    payload = list_response.json()
    assert len(payload["tasks"]) == 1
    task = payload["tasks"][0]
    assert task["task_id"] == task_id
    assert task["logs"]
    assert "phase" in task
    assert "progress_percent" in task
    assert "speed_bytes_per_sec" in task


def test_sync_can_keep_local_artifact(monkeypatch, tmp_path) -> None:
    config_path = write_config(
        tmp_path,
        extra_lines=[
            "keep_downloaded_files: true",
            f"download_cache_dir: {tmp_path / 'cache'}",
        ],
    )
    monkeypatch.setenv("DOCKER_IMG_DOWNLOADER_CONFIG", str(config_path))

    app = create_app()
    manager = app.state.manager
    recorded_commands: list[list[str]] = []

    def fake_run(task_id, command, failure_prefix, **kwargs):
        recorded_commands.append(command)
        if "pull" in command:
            manager._update_task(
                task_id,
                phase="exporting",
                append_log="artifact exported",
                local_artifact_path=command[-1],
            )
        return None

    monkeypatch.setattr(manager, "_run_command", fake_run)
    client = TestClient(app)

    response = client.post("/sync", json={"source_image": "docker.io/library/nginx:1.27.4"})
    assert response.status_code == 200
    task_id = response.json()["task_id"]

    detail_response = client.get(f"/api/tasks/{task_id}")
    assert detail_response.status_code == 200
    payload = detail_response.json()

    assert payload["local_artifact_path"]
    assert payload["local_artifact_path"].endswith(".tar")
    assert any(command[1] == "pull" for command in recorded_commands)
