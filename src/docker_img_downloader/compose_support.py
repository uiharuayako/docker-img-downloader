from __future__ import annotations

from pathlib import Path

import yaml

from .image_ref import ImageReferenceError


class ComposeImageError(ValueError):
    pass


def extract_images_from_compose_data(data: dict) -> list[str]:
    if not isinstance(data, dict):
        raise ComposeImageError("Compose content must be a YAML mapping.")

    services = data.get("services")
    if not isinstance(services, dict):
        raise ComposeImageError("Compose content must contain a services mapping.")

    images: list[str] = []
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        image = service.get("image")
        if image is None:
            continue
        if not isinstance(image, str) or not image.strip():
            raise ComposeImageError(f"Service '{service_name}' has an invalid image value.")
        images.append(image.strip())

    return dedupe_preserve_order(images)


def extract_images_from_compose_text(compose_text: str) -> list[str]:
    data = yaml.safe_load(compose_text) or {}
    return extract_images_from_compose_data(data)


def extract_images_from_compose_file(path: str | Path) -> list[str]:
    compose_path = Path(path).expanduser().resolve()
    with compose_path.open("r", encoding="utf-8") as file:
        return extract_images_from_compose_text(file.read())


def normalize_compose_image(value: str) -> str:
    image = value.strip()
    if not image:
        raise ComposeImageError("Compose image value cannot be empty.")
    if "@" in image:
        raise ComposeImageError("Digest-based compose image references are not supported in v1.")

    name_part, tag = split_image_tag(image)
    if "/" not in name_part:
        repository = f"library/{name_part}"
        return f"docker.io/{repository}:{tag}"

    first_part, rest = name_part.split("/", 1)
    if "." in first_part or ":" in first_part or first_part == "localhost":
        return f"{name_part}:{tag}"

    return f"docker.io/{name_part}:{tag}"


def normalize_compose_images(images: list[str]) -> list[str]:
    normalized: list[str] = []
    for image in images:
        normalized.append(normalize_compose_image(image))
    return dedupe_preserve_order(normalized)


def split_image_tag(image: str) -> tuple[str, str]:
    last_slash = image.rfind("/")
    last_colon = image.rfind(":")
    if last_colon > last_slash:
        name_part = image[:last_colon]
        tag = image[last_colon + 1 :]
        if not tag:
            raise ComposeImageError("Compose image tag cannot be empty.")
        return name_part, tag
    return image, "latest"


def dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def normalize_compose_file_images(path: str | Path) -> list[str]:
    try:
        images = extract_images_from_compose_file(path)
        return normalize_compose_images(images)
    except ImageReferenceError as exc:
        raise ComposeImageError(str(exc)) from exc
