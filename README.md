# docker-img-downloader

按需回填镜像方案：`Linux` 发起拉取，若 `Harbor` 缺失镜像，则自动调用 `Windows` 同步服务，通过 `crane` 从公网或镜像站下载并推送到 Harbor，然后 Linux 自动重试完成 `docker pull`。

## 组成

- `imgpull`：Linux 侧包装命令，输入完整公网镜像地址。
- `imgsync-compose`：根据 `docker-compose.yaml` 批量提取镜像并触发 Windows 上传。
- `harbor-sync-service`：Windows 侧轻量 HTTP 服务。
- Web Dashboard：Windows 服务自带网页，可查看任务状态、阶段、速度、最近日志。
- `docker_img_downloader.image_ref`：Linux / Windows 共用的镜像解析与 Harbor 映射规则。
- `docker_img_downloader.compose_support`：Compose 镜像提取与规范化逻辑。

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

## 完整配置流程

### 1. 下载并放置 `crane.exe`

- 下载 `crane.exe`
- 放到固定位置，例如 `C:\tools\crane.exe`
- 如果公司安全策略限制 `crane` 访问加密文本文件，优先保证：
  - 项目目录本身不做文件加密
  - `%USERPROFILE%\\.docker\\config.json` 的策略不会阻止 `crane auth login`

### 2. 复制并编辑服务配置

- 把 `config/service.example.yaml` 复制为 `config/service.yaml`
- 填写 Harbor、端口、镜像站、`crane.exe` 路径

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
keep_downloaded_files: false
download_cache_dir: ./data/cache
```

如果不配置 `allowed_source_registries`，则默认允许任意来源 registry 的镜像。

推荐使用 `.env` 文件，而不是每次手动在 PowerShell 中设置环境变量。

在项目根目录创建 `.env`，可以直接参考：`/Users/uiharu/code/docker_img_downloader/.env.example:1`

```dotenv
HARBOR_USERNAME=your-email@company.com
HARBOR_PASSWORD=your-harbor-password
HTTP_PROXY=http://proxy.example.com:7890
HTTPS_PROXY=http://proxy.example.com:7890
NO_PROXY=harbor.intra.local,127.0.0.1,localhost
```

服务启动时会自动按下面顺序查找 `.env`：

- `config/.env`
- 项目根目录 `.env`

如果找到了，就会自动加载；因此 Windows 端**不需要每次运行都手动配置环境变量**。

如果你仍想临时覆盖，也可以继续在 PowerShell 里设置同名环境变量；显式设置的系统环境变量优先级更高。

也可以继续手动设置环境变量：

```powershell
$env:HARBOR_USERNAME = "robot$mirror"
$env:HARBOR_PASSWORD = "your-token"
```

如果你的 Harbor 用户名就是邮箱，直接填邮箱即可，例如：

```powershell
$env:HARBOR_USERNAME = "your-email@company.com"
$env:HARBOR_PASSWORD = "your-harbor-password"
```

如果 `docker.io` 访问需要走镜像站，可在 `config/service.yaml` 中配置一个或多个镜像站，服务会按顺序尝试，全部失败后再回退到原始上游：

```yaml
registry_mirrors:
  docker.io:
    - docker.m.daocloud.io
    - dockerproxy.cn
```

这里的镜像站应当是兼容 Registry API 的站点，写法是“registry host”，不要带 `https://`。

如果希望 Windows 在同步完成后额外保留一份本地镜像 tar，可打开下面两个配置：

```yaml
keep_downloaded_files: true
download_cache_dir: ./data/cache
```

行为说明：

- 默认是 `false`，也就是只推送到 Harbor，不在本地保留文件
- 打开后，服务会在镜像成功推送到 Harbor 后，再执行一次 `crane pull` 导出 tar
- 导出的 tar 会保存在 `download_cache_dir`
- 任务详情和网页看板里会显示 `local_artifact_path`
- 这不是“临时文件不删除”，而是显式增加一份本地导出产物

如果访问公网需要代理：

```powershell
$env:HTTP_PROXY = "http://proxy.example.com:7890"
$env:HTTPS_PROXY = "http://proxy.example.com:7890"
$env:NO_PROXY = "harbor.intra.local,127.0.0.1,localhost"
```

### 3. 启动 Windows 服务

- 在项目根目录执行：
- 推荐先准备好 `.env`
- 然后在项目根目录执行：

```powershell
python -m docker_img_downloader.sync_service --config config/service.yaml
```

- 或者：

```powershell
harbor-sync-service --config config/service.yaml
```

启动后可直接打开面板：

- `http://127.0.0.1:8080/`
- 局域网其他机器可访问：`http://<windows-ip>:8080/`

面板能力：

- 查看任务列表、状态、阶段、当前来源镜像
- 查看本地保留文件路径（如果启用了 `keep_downloaded_files`）
- 查看最近进度百分比、累计字节、瞬时速度
- 查看最近日志，便于判断是代理、上游源还是 Harbor 推送异常
- 直接在网页里提交单镜像同步
- 直接在网页里粘贴 Compose YAML，或上传 `docker-compose.yaml` / `.yml` 文件后再提交
- 直接在网页里手工构造 JSON，请求 `POST /sync` 做接口调试

说明：

- 实时速度和字节进度依赖 `crane` 实际输出；如果某些版本/场景未输出数值，面板仍会显示阶段和日志
- 当前实现通过流式读取 `crane` 标准输出获得实时状态，不再等命令结束后才更新

### 4. Linux 侧准备

- 安装项目：

```bash
python3 -m pip install .
```

- 登录 Harbor：

```bash
docker login harbor.intra.local
```

- 如果 Harbor 查询 manifest 也要求认证，可额外导出：

```bash
export HARBOR_USERNAME='your-email@company.com'
export HARBOR_PASSWORD='your-harbor-password'
```

健康检查：

```powershell
curl http://127.0.0.1:8080/healthz
```

查看任务列表接口：

```powershell
curl http://127.0.0.1:8080/api/tasks
```

## Linux 侧使用

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

## 根据 docker-compose.yaml 批量同步

支持从 `docker-compose.yaml` 提取 `services.*.image`，自动规范化后批量触发上传。

规范化规则：

- `nginx:1.27.4` → `docker.io/library/nginx:1.27.4`
- `redis` → `docker.io/library/redis:latest`
- `bitnami/redis:7` → `docker.io/bitnami/redis:7`
- `ghcr.io/example/app:v1` 保持不变

### 方式一：本地/ Linux 侧用 CLI 触发

```bash
imgsync-compose \
  --compose-file ./docker-compose.yaml \
  --windows-sync-url http://10.0.0.20:8080 \
  --wait
```

这条命令会：

1. 读取本地 `docker-compose.yaml`
2. 提取 `services.*.image`
3. 自动补全默认 registry / namespace / tag
4. 调用 Windows `POST /sync/compose`
5. `--wait` 时轮询所有任务直到完成

### 方式二：直接调用 Windows API

接口：

- `POST /sync/compose`

请求体支持两种形式：

```json
{"compose_yaml":"services:\n  web:\n    image: nginx:1.27.4\n"}
```

或

```json
{"compose_file_path":"C:/path/to/docker-compose.yaml"}
```

注意：`compose_file_path` 只适合 Windows 服务本机调试；如果 compose 文件在 Linux 上，推荐传 `compose_yaml`。

## 本地调试流程

下面流程适合先在 Windows 本机把链路跑通，再接 Linux。

### 1. 启动服务

```powershell
python -m docker_img_downloader.sync_service --config config/service.yaml
```

### 2. 手动提交单镜像任务

```powershell
curl.exe -X POST http://127.0.0.1:8080/sync ^
  -H "Content-Type: application/json" ^
  -d "{\"source_image\":\"docker.io/library/busybox:stable\"}"
```

返回示例：

```json
{
  "task_id": "3e5c0b9d...",
  "status": "queued",
  "source_image": "docker.io/library/busybox:stable",
  "target_image": "harbor.intra.local/mirror/dockerhub/library/busybox:stable",
  "message": "Task queued."
}
```

### 3. 轮询任务详情

把上一步返回的 `task_id` 替换进去：

```powershell
curl.exe http://127.0.0.1:8080/api/tasks/3e5c0b9d...
```

重点字段：

- `status`：`queued / running / succeeded / failed`
- `phase`：如 `login / copying / pushing / succeeded / failed`
- `progress_percent`：最近一次解析到的百分比
- `speed_bytes_per_sec`：最近一次解析到的速度
- `logs`：最近日志滚动窗口

### 4. 直接打开网页看板

浏览器访问：

```text
http://127.0.0.1:8080/
```

如果你主要是给内网 Linux 用户使用，建议把这个地址告诉他们，让他们直接看任务是否在下载、速度是否异常、是否卡在 Harbor 推送阶段。

### 5. 手动调试 Compose 批量同步

```powershell
curl.exe -X POST http://127.0.0.1:8080/sync/compose ^
  -H "Content-Type: application/json" ^
  -d "{\"compose_yaml\":\"services:\n  web:\n    image: nginx:1.27.4\n  redis:\n    image: redis:7\"}"
```

### 6. Linux 联调

Windows 服务正常后，在 Linux 上执行：

```bash
imgpull docker.io/library/busybox:stable \
  --harbor-registry harbor.intra.local \
  --harbor-project mirror \
  --windows-sync-url http://10.0.0.20:8080
```

或批量同步：

```bash
imgsync-compose \
  --compose-file ./docker-compose.yaml \
  --windows-sync-url http://10.0.0.20:8080 \
  --wait
```

此时可以同时打开 Windows 面板观察任务进度，确认 Linux 是否只负责触发、真正的外网下载是否发生在 Windows 侧。

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

### `POST /sync/compose`

请求：

```json
{
  "compose_yaml": "services:\n  web:\n    image: nginx:1.27.4\n  api:\n    image: ghcr.io/example/app:v1\n"
}
```

响应：

```json
{
  "images": [
    "docker.io/library/nginx:1.27.4",
    "ghcr.io/example/app:v1"
  ],
  "tasks": [
    {
      "task_id": "task-1",
      "status": "queued",
      "source_image": "docker.io/library/nginx:1.27.4",
      "target_image": "harbor.intra.local/mirror/dockerhub/library/nginx:1.27.4",
      "message": "Task queued.",
      "created_at": "2026-03-30T00:00:00+00:00",
      "started_at": null,
      "finished_at": null
    }
  ]
}

```

## 本地调试流程

推荐按下面顺序调试，问题定位最快。

### 1. 仅测服务是否启动

如果使用 `.env`，先复制示例文件：

```powershell
Copy-Item .env.example .env
```

然后把里面的 Harbor 和代理参数改成你的实际值，再启动服务。

```powershell
curl http://127.0.0.1:8080/healthz
```

预期：

```json
{"status":"ok"}
```

### 2. 手动构造单镜像同步请求

PowerShell：

```powershell
$body = @{ source_image = "docker.io/library/busybox:stable" } | ConvertTo-Json
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8080/sync" `
  -ContentType "application/json" `
  -Body $body
```

或者：

```powershell
curl -X POST http://127.0.0.1:8080/sync `
  -H "Content-Type: application/json" `
  -d "{\"source_image\":\"docker.io/library/busybox:stable\"}"
```

### 3. 手动轮询任务状态

```powershell
curl http://127.0.0.1:8080/tasks/<task_id>
```

### 4. 手动构造 compose 批量请求

PowerShell：

```powershell
$compose = @"
services:
  web:
    image: nginx:1.27.4
  redis:
    image: redis
"@

$body = @{ compose_yaml = $compose } | ConvertTo-Json
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8080/sync/compose" `
  -ContentType "application/json" `
  -Body $body
```

或者：

```powershell
curl -X POST http://127.0.0.1:8080/sync/compose `
  -H "Content-Type: application/json" `
  -d "{\"compose_yaml\":\"services:\n  web:\n    image: nginx:1.27.4\n  redis:\n    image: redis\n\"}"
```

### 5. 直接调 CLI 验证 compose 触发

```bash
imgsync-compose \
  --compose-file ./docker-compose.yaml \
  --windows-sync-url http://127.0.0.1:8080 \
  --wait
```

### 6. Linux 端验证完整链路

```bash
imgpull docker.io/library/busybox:stable \
  --harbor-registry harbor.intra.local \
  --harbor-project mirror \
  --windows-sync-url http://10.0.0.20:8080 \
  --harbor-username 'your-email@company.com' \
  --harbor-password 'your-harbor-password'
```

### 推荐联调顺序

- 先测 `GET /healthz`
- 再测 `POST /sync`
- 确认 Harbor 里已有目标镜像
- 再测 `POST /sync/compose`
- 最后测 Linux 侧 `imgpull` 和 `imgsync-compose`

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
