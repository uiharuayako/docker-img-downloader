from fastapi.testclient import TestClient

from docker_img_downloader.sync_service import create_app


def test_healthz(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "service.yaml"
    config_path.write_text(
        "\n".join(
            [
                "harbor_registry: harbor.intra.local",
                "harbor_project: mirror",
                "harbor_username: user",
                "harbor_password: pass",
                "listen_host: 127.0.0.1",
                "listen_port: 8080",
                "platform: linux/amd64",
                "allowed_source_registries:",
                "  - docker.io",
                "  - ghcr.io",
                "registry_mirrors:",
                "  docker.io:",
                "    - docker.m.daocloud.io",
                "crane_path: crane.exe",
                "task_store_path: ./tasks.json",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCKER_IMG_DOWNLOADER_CONFIG", str(config_path))

    app = create_app()
    client = TestClient(app)

    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_sync_reuses_active_task_with_mirror_config(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "service.yaml"
    config_path.write_text(
        "\n".join(
            [
                "harbor_registry: harbor.intra.local",
                "harbor_project: mirror",
                "harbor_username: user",
                "harbor_password: pass",
                "listen_host: 127.0.0.1",
                "listen_port: 8080",
                "platform: linux/amd64",
                "allowed_source_registries:",
                "  - docker.io",
                "registry_mirrors:",
                "  docker.io:",
                "    - docker.m.daocloud.io",
                "crane_path: crane.exe",
                f"task_store_path: {tmp_path / 'tasks.json'}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCKER_IMG_DOWNLOADER_CONFIG", str(config_path))

    app = create_app()
    manager = app.state.manager

    def slow_run(*args, **kwargs):
        import time

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
    config_path = tmp_path / "service.yaml"
    config_path.write_text(
        "\n".join(
            [
                "harbor_registry: harbor.intra.local",
                "harbor_project: mirror",
                "harbor_username: user",
                "harbor_password: pass",
                "listen_host: 127.0.0.1",
                "listen_port: 8080",
                "platform: linux/amd64",
                "allowed_source_registries:",
                "  - docker.io",
                "  - ghcr.io",
                "crane_path: crane.exe",
                f"task_store_path: {tmp_path / 'tasks-compose.json'}",
            ]
        ),
        encoding="utf-8",
    )
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
