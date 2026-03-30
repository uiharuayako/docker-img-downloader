from docker_img_downloader.compose_support import (
    extract_images_from_compose_text,
    normalize_compose_file_images,
    normalize_compose_images,
)


def test_extract_images_from_compose_text() -> None:
    compose = """
services:
  web:
    image: nginx:1.27.4
  api:
    image: ghcr.io/example/app:v1
  worker:
    build: .
"""
    images = extract_images_from_compose_text(compose)
    assert images == ["nginx:1.27.4", "ghcr.io/example/app:v1"]


def test_normalize_compose_images() -> None:
    normalized = normalize_compose_images(["nginx:1.27.4", "bitnami/redis:7", "ghcr.io/example/app:v1"])
    assert normalized == [
        "docker.io/library/nginx:1.27.4",
        "docker.io/bitnami/redis:7",
        "ghcr.io/example/app:v1",
    ]


def test_normalize_compose_file_images(tmp_path) -> None:
    compose_path = tmp_path / "docker-compose.yaml"
    compose_path.write_text(
        """
services:
  web:
    image: redis
  api:
    image: docker.io/library/busybox:stable
""",
        encoding="utf-8",
    )
    normalized = normalize_compose_file_images(compose_path)
    assert normalized == [
        "docker.io/library/redis:latest",
        "docker.io/library/busybox:stable",
    ]
