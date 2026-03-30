# docker-img-downloader

按需回填镜像方案：`Linux` 发起拉取，若 `Harbor` 缺失镜像，则自动调用 `Windows` 同步服务，通过 `crane` 从公网下载并推送到 Harbor，然后 Linux 自动重试完成 `docker pull`。

## 组成

- `imgpull`：Linux 侧包装命令，输入完整公网镜像地址。
- `harbor-sync-service`：Windows 侧轻量 HTTP 服务。
- `docker_img_downloader.image_ref`：Linux / Windows 共用的镜像解析与 Harbor 映射规则。

## Harbor 映射规则

- `docker.io/library/nginx:1.27.4` → `<harbor>/<project>/dockerhub/library/nginx:1.27.4`
- `ghcr.io/org/app:v1` → `<harbor>/<project>/ghcr/org/app:v1`

对于未显式内建别名的 registry，会将域名规范化为路径前缀，例如 `quay.io` → `quay-io`。

## 安装

### Linux

```bash
python3 -m pip install .
```

如果只想本地运行而不安装入口点：

```bash
python3 -m pip install -e .
```

### Windows

1. 安装 `Python 3.12`
2. 下载 `crane.exe` 并放到本地路径，例如 `C:\tools\crane.exe`
3. 安装项目依赖：

```powershell
py -3.12 -m pip install .
```

## Windows 侧配置

复制 `config/service.example.yaml` 为 `config/service.yaml`，并按环境修改：

```yaml
harbor_registry: harbor.intra.local
harbor_project: mirror
harbor_username: ${HARBOR_USERNAME}
harbor_password: ${HARBOR_PASSWORD}
listen_host: 0.0.0.0
listen_port: 8080
platform: linux/amd64
allowed_source_registries:
  - docker.io
  - ghcr.io
crane_path: C:/tools/crane.exe
harbor_scheme: https
verify_tls: true
task_store_path: ./data/tasks.json
```

推荐将 Harbor 凭据放到环境变量中：

```powershell
$env:HARBOR_USERNAME = "robot$mirror"
$env:HARBOR_PASSWORD = "your-token"
```

如果 `docker.io` 访问需要走镜像站，可在 `config/service.yaml` 中配置一个或多个镜像站，服务会按顺序尝试，全部失败后再回退到原始上游：

```yaml
registry_mirrors:
  docker.io:
    - docker.m.daocloud.io
    - dockerproxy.cn
```

这里的镜像站应当是兼容 Registry API 的站点，写法是“registry host”，不要带 `https://`。

如果访问公网需要代理：

```powershell
$env:HTTP_PROXY = "http://proxy.example.com:7890"
$env:HTTPS_PROXY = "http://proxy.example.com:7890"
$env:NO_PROXY = "harbor.intra.local,127.0.0.1,localhost"
```

启动服务：

```powershell
python -m docker_img_downloader.sync_service --config config/service.yaml
```

健康检查：

```powershell
curl http://127.0.0.1:8080/healthz
```

## Linux 侧使用

先登录 Harbor：

```bash
docker login harbor.intra.local
```

如果 `imgpull` 未安装到 PATH，可以使用：

```bash
./scripts/imgpull.sh docker.io/library/busybox:stable \
  --harbor-registry harbor.intra.local \
  --harbor-project mirror \
  --windows-sync-url http://10.0.0.20:8080
```

如果已安装入口点：

```bash
imgpull docker.io/library/busybox:stable \
  --harbor-registry harbor.intra.local \
  --harbor-project mirror \
  --windows-sync-url http://10.0.0.20:8080
```

Harbor 使用自签名证书时：

```bash
imgpull docker.io/library/busybox:stable \
  --harbor-registry harbor.intra.local \
  --windows-sync-url http://10.0.0.20:8080 \
  --insecure
```

Linux 侧 `imgpull` 默认行为：

1. 先检查 Harbor manifest 是否存在
2. 若存在，直接 `docker pull`
3. 若不存在，调用 Windows `POST /sync`
4. 轮询 `GET /tasks/{task_id}`
5. 任务成功后执行 `docker pull`

## Docker 镜像站支持

- Linux 侧输入仍然保持原始镜像名，例如 `docker.io/library/nginx:1.27.4`
- Harbor 内部命名仍然按原始来源映射为 `dockerhub/...`
- 只有 Windows 下载阶段会根据 `registry_mirrors.docker.io` 改写抓取来源
- 如果配置了多个镜像站，Windows 会依次尝试，最后回退到原始 `docker.io`

## API

### `POST /sync`

请求：

```json
{"source_image":"docker.io/library/nginx:1.27.4"}
```

响应：

```json
{
  "task_id": "0d6b0c0d4ea34a2a8dd4d4c7d1d4dbe6",
  "status": "queued",
  "source_image": "docker.io/library/nginx:1.27.4",
  "target_image": "harbor.intra.local/mirror/dockerhub/library/nginx:1.27.4",
  "message": "Task queued.",
  "created_at": "2026-03-30T00:00:00+00:00",
  "started_at": null,
  "finished_at": null
}
```

### `GET /tasks/{task_id}`

返回当前任务状态，`status` 取值为：

- `queued`
- `running`
- `succeeded`
- `failed`

## Task Scheduler 建议

如果希望 Windows 开机自启动，可创建一个计划任务，执行：

```powershell
python -m docker_img_downloader.sync_service --config C:\path\to\service.yaml
```

## 测试

```bash
python3 -m pip install -e '.[dev]'
pytest
```
