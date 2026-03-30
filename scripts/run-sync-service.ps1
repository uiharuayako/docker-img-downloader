param(
    [string]$ConfigPath = "config/service.yaml"
)

$ErrorActionPreference = "Stop"
python -m docker_img_downloader.sync_service --config $ConfigPath
