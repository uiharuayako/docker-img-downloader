from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urljoin

import requests

from .image_ref import (
    ImageReferenceError,
    build_harbor_manifest_path,
    build_target_image,
    parse_image_reference,
)


MANIFEST_ACCEPT_HEADER = ",".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
    ]
)


class ImgPullError(RuntimeError):
    pass


def parse_duration(value: str) -> int:
    text = value.strip().lower()
    if text.isdigit():
        return int(text)
    units = {"s": 1, "m": 60, "h": 3600}
    suffix = text[-1:]
    if suffix not in units:
        raise argparse.ArgumentTypeError("Duration must end with s, m, or h.")
    number = text[:-1]
    if not number.isdigit():
        raise argparse.ArgumentTypeError("Duration must start with an integer value.")
    return int(number) * units[suffix]


def manifest_exists(
    *,
    harbor_registry: str,
    harbor_project: str,
    source_image: str,
    harbor_scheme: str,
    verify_tls: bool,
    harbor_username: str | None,
    harbor_password: str | None,
    request_timeout: int = 10,
) -> bool:
    repo_name, tag = build_harbor_manifest_path(source_image, harbor_project)
    url = f"{harbor_scheme}://{harbor_registry.rstrip('/')}/v2/{repo_name}/manifests/{tag}"
    auth = (harbor_username, harbor_password) if harbor_username and harbor_password else None
    response = requests.head(
        url,
        headers={"Accept": MANIFEST_ACCEPT_HEADER},
        auth=auth,
        timeout=request_timeout,
        verify=verify_tls,
    )
    if response.status_code == 200:
        return True
    if response.status_code == 404:
        return False
    if response.status_code in (401, 403):
        raise ImgPullError("Harbor authentication failed while checking the manifest.")
    raise ImgPullError(f"Unexpected Harbor response {response.status_code} while checking the manifest.")


def request_sync(windows_sync_url: str, source_image: str, request_timeout: int = 15) -> dict[str, Any]:
    url = urljoin(windows_sync_url.rstrip("/") + "/", "sync")
    try:
        response = requests.post(url, json={"source_image": source_image}, timeout=request_timeout)
    except requests.RequestException as exc:
        raise ImgPullError(f"Windows sync service is unreachable: {exc}") from exc

    if response.status_code != 200:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise ImgPullError(f"Windows sync request failed: {detail}")

    payload = response.json()
    if "task_id" not in payload:
        raise ImgPullError("Windows sync service returned an invalid response without task_id.")
    return payload


def wait_for_task(windows_sync_url: str, task_id: str, timeout_seconds: int, poll_interval: int) -> dict[str, Any]:
    url = urljoin(windows_sync_url.rstrip("/") + "/", f"tasks/{task_id}")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = requests.get(url, timeout=15)
        except requests.RequestException as exc:
            raise ImgPullError(f"Failed to poll Windows sync task: {exc}") from exc

        if response.status_code == 404:
            raise ImgPullError("Windows sync task disappeared before completion.")
        if response.status_code != 200:
            raise ImgPullError(f"Unexpected Windows sync task response: {response.status_code}")

        payload = response.json()
        status = payload.get("status")
        if status == "succeeded":
            return payload
        if status == "failed":
            message = payload.get("message") or "Windows sync task failed."
            raise ImgPullError(f"Windows sync failed: {message}")
        time.sleep(poll_interval)

    raise ImgPullError("Timed out while waiting for Windows to download and upload the image.")


def docker_pull(target_image: str) -> None:
    completed = subprocess.run(["docker", "pull", target_image], text=True, check=False)
    if completed.returncode != 0:
        raise ImgPullError(f"Local docker pull failed for {target_image}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pull images from Harbor with on-demand Windows backfill.")
    parser.add_argument("source_image", help="Full upstream image reference, for example docker.io/library/nginx:1.27.4")
    parser.add_argument("--timeout", type=parse_duration, default=600, help="Maximum wait time, such as 600, 600s, 10m, or 1h")
    parser.add_argument("--poll-interval", type=parse_duration, default=5, help="Polling interval, such as 5, 5s, or 1m")
    parser.add_argument("--harbor-registry", required=True, help="Harbor registry host, for example harbor.intra.local")
    parser.add_argument("--harbor-project", default=os.environ.get("HARBOR_PROJECT", "mirror"), help="Harbor project name")
    parser.add_argument("--harbor-scheme", default=os.environ.get("HARBOR_SCHEME", "https"), choices=["http", "https"], help="Harbor URL scheme")
    parser.add_argument("--windows-sync-url", required=True, help="Base URL of the Windows sync service, for example http://10.0.0.10:8080")
    parser.add_argument("--harbor-username", default=os.environ.get("HARBOR_USERNAME"), help="Harbor username, defaults to HARBOR_USERNAME")
    parser.add_argument("--harbor-password", default=os.environ.get("HARBOR_PASSWORD"), help="Harbor password, defaults to HARBOR_PASSWORD")
    parser.add_argument("--request-timeout", type=int, default=10, help="HTTP timeout for single Harbor checks")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification for Harbor checks")
    return parser


def run(args: argparse.Namespace) -> int:
    try:
        parse_image_reference(args.source_image)
    except ImageReferenceError as exc:
        raise ImgPullError(str(exc)) from exc

    target_image = build_target_image(
        source_image=args.source_image,
        harbor_registry=args.harbor_registry,
        harbor_project=args.harbor_project,
    )

    exists = manifest_exists(
        harbor_registry=args.harbor_registry,
        harbor_project=args.harbor_project,
        source_image=args.source_image,
        harbor_scheme=args.harbor_scheme,
        verify_tls=not args.insecure,
        harbor_username=args.harbor_username,
        harbor_password=args.harbor_password,
        request_timeout=args.request_timeout,
    )

    if not exists:
        sync_response = request_sync(args.windows_sync_url, args.source_image)
        wait_for_task(
            windows_sync_url=args.windows_sync_url,
            task_id=sync_response["task_id"],
            timeout_seconds=args.timeout,
            poll_interval=args.poll_interval,
        )

    docker_pull(target_image)
    print(target_image)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except ImgPullError as exc:
        print(f"imgpull: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"imgpull: network error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
