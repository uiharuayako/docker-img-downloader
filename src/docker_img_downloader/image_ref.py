from __future__ import annotations

import re
from dataclasses import dataclass


class ImageReferenceError(ValueError):
    pass


_SANITIZE_PATTERN = re.compile(r"[^a-z0-9]+")
_REGISTRY_ALIASES = {
    "docker.io": "dockerhub",
    "ghcr.io": "ghcr",
}


@dataclass(frozen=True, slots=True)
class ImageReference:
    registry: str
    repository: str
    tag: str

    @property
    def source_image(self) -> str:
        return f"{self.registry}/{self.repository}:{self.tag}"


def parse_image_reference(value: str) -> ImageReference:
    image = value.strip()
    if not image:
        raise ImageReferenceError("Image reference cannot be empty.")
    if "@" in image:
        raise ImageReferenceError("Digest-based references are not supported in v1.")

    first_slash = image.find("/")
    if first_slash <= 0:
        raise ImageReferenceError("Image reference must include an explicit registry host.")

    registry = image[:first_slash]
    if "." not in registry and ":" not in registry and registry != "localhost":
        raise ImageReferenceError("Image reference must include an explicit registry host.")

    last_slash = image.rfind("/")
    last_colon = image.rfind(":")
    if last_colon <= last_slash:
        raise ImageReferenceError("Image reference must include an explicit tag.")

    repository = image[first_slash + 1 : last_colon]
    tag = image[last_colon + 1 :]

    if not repository:
        raise ImageReferenceError("Repository path cannot be empty.")
    if not tag:
        raise ImageReferenceError("Tag cannot be empty.")

    return ImageReference(registry=registry.lower(), repository=repository, tag=tag)


def registry_namespace(registry: str) -> str:
    alias = _REGISTRY_ALIASES.get(registry.lower())
    if alias:
        return alias
    sanitized = _SANITIZE_PATTERN.sub("-", registry.lower()).strip("-")
    if not sanitized:
        raise ImageReferenceError(f"Could not derive a Harbor namespace from registry '{registry}'.")
    return sanitized


def build_target_repo(image: ImageReference) -> str:
    return f"{registry_namespace(image.registry)}/{image.repository}"


def build_target_image(
    source_image: str,
    harbor_registry: str,
    harbor_project: str,
) -> str:
    parsed = parse_image_reference(source_image)
    target_repo = build_target_repo(parsed)
    registry = harbor_registry.rstrip("/")
    project = harbor_project.strip("/")
    return f"{registry}/{project}/{target_repo}:{parsed.tag}"


def build_harbor_manifest_path(source_image: str, harbor_project: str) -> tuple[str, str]:
    parsed = parse_image_reference(source_image)
    target_repo = build_target_repo(parsed)
    return f"{harbor_project.strip('/')}/{target_repo}", parsed.tag


def replace_registry(source_image: str, new_registry: str) -> str:
    parsed = parse_image_reference(source_image)
    registry = new_registry.strip().rstrip("/")
    if not registry:
        raise ImageReferenceError("Replacement registry cannot be empty.")
    return f"{registry}/{parsed.repository}:{parsed.tag}"
