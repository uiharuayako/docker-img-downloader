from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from .compose_support import ComposeImageError, normalize_compose_file_images
from .imgpull import ImgPullError, wait_for_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync all images required by a docker-compose YAML file.")
    parser.add_argument("--compose-file", required=True, help="Path to docker-compose YAML")
    parser.add_argument("--windows-sync-url", required=True, help="Base URL of the Windows sync service")
    parser.add_argument("--wait", action="store_true", help="Wait for all tasks to finish")
    parser.add_argument("--timeout", type=int, default=600, help="Maximum wait time per task in seconds")
    parser.add_argument("--poll-interval", type=int, default=5, help="Polling interval in seconds")
    return parser


def request_compose_sync(windows_sync_url: str, compose_yaml: str) -> dict[str, Any]:
    url = urljoin(windows_sync_url.rstrip("/") + "/", "sync/compose")
    try:
        response = requests.post(url, json={"compose_yaml": compose_yaml}, timeout=30)
    except requests.RequestException as exc:
        raise ImgPullError(f"Windows compose sync service is unreachable: {exc}") from exc

    if response.status_code != 200:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise ImgPullError(f"Windows compose sync request failed: {detail}")
    return response.json()


def run(args: argparse.Namespace) -> int:
    compose_path = Path(args.compose_file).expanduser().resolve()
    compose_yaml = compose_path.read_text(encoding="utf-8")
    images = normalize_compose_file_images(compose_path)
    payload = request_compose_sync(args.windows_sync_url, compose_yaml)

    tasks = payload.get("tasks", [])
    if len(tasks) != len(images):
        raise ImgPullError("Compose sync response does not match the number of normalized images.")

    if args.wait:
        for task in tasks:
            wait_for_task(
                windows_sync_url=args.windows_sync_url,
                task_id=task["task_id"],
                timeout_seconds=args.timeout,
                poll_interval=args.poll_interval,
            )

    for image in payload.get("images", []):
        print(image)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except (ImgPullError, ComposeImageError) as exc:
        print(f"imgsync-compose: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
