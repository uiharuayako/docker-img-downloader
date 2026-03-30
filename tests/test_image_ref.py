from docker_img_downloader.image_ref import (
    ImageReferenceError,
    build_harbor_manifest_path,
    build_target_image,
    parse_image_reference,
    replace_registry,
)
from docker_img_downloader.imgpull import parse_duration


def test_parse_public_image_reference() -> None:
    parsed = parse_image_reference("docker.io/library/nginx:1.27.4")
    assert parsed.registry == "docker.io"
    assert parsed.repository == "library/nginx"
    assert parsed.tag == "1.27.4"


def test_parse_requires_explicit_registry() -> None:
    try:
        parse_image_reference("nginx:latest")
    except ImageReferenceError as exc:
        assert "explicit registry host" in str(exc)
    else:
        raise AssertionError("Expected explicit registry validation to fail.")


def test_parse_rejects_digest_only() -> None:
    try:
        parse_image_reference("docker.io/library/nginx@sha256:abc")
    except ImageReferenceError as exc:
        assert "Digest-based references" in str(exc)
    else:
        raise AssertionError("Expected digest validation to fail.")


def test_target_image_mapping() -> None:
    target = build_target_image(
        "docker.io/library/nginx:1.27.4",
        harbor_registry="harbor.intra.local",
        harbor_project="mirror",
    )
    assert target == "harbor.intra.local/mirror/dockerhub/library/nginx:1.27.4"


def test_manifest_path_mapping() -> None:
    repo, tag = build_harbor_manifest_path("ghcr.io/org/app:v1", "mirror")
    assert repo == "mirror/ghcr/org/app"
    assert tag == "v1"


def test_parse_duration_variants() -> None:
    assert parse_duration("600") == 600
    assert parse_duration("10m") == 600
    assert parse_duration("1h") == 3600


def test_replace_registry_keeps_repo_and_tag() -> None:
    replaced = replace_registry("docker.io/library/nginx:1.27.4", "docker.m.daocloud.io")
    assert replaced == "docker.m.daocloud.io/library/nginx:1.27.4"
