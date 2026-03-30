#!/usr/bin/env bash
set -euo pipefail

python3 -m docker_img_downloader.imgpull "$@"
