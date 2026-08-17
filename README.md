# 系统面板 (Go 二进制) Docker 部署

系统面板的 Docker 镜像，以 Go 二进制方式运行，支持 **数据持久化**。

## 目录结构

```
go二进制方式/
├── Dockerfile    # 镜像构建(含持久化卷 /data)
├── start.sh      # 启动脚本(下载二进制、启动面板、可选 argo/keepalive)
└── README.md     # 本文档
```

## 快速开始

### 1. 构建镜像

```bash
docker build -t system-panel .
```

### 2. 运行容器（推荐：挂载持久化目录）

```bash
docker run -d \
  --name system-panel \
  -p 8080:8080 \
  -v /宿主机/持久化目录:/data \
  -e PANEL_PASSWORD='你的密码' \
  --restart unless-stopped \
  system-panel
```

启动后访问 `http://服务器IP:8080`。

> **注意**：`/data` 是面板的数据根目录（`BASE_DIR`），面板文件管理里**上传/创建的文件**都存这里。
> 挂载宿主机目录后，容器删除重建、数据依然保留。不挂载则数据随容器销毁丢失。

## 数据持久化

| 内容 | 位置 | 是否持久化 |
|------|------|-----------|
| 面板文件管理上传/创建的文件 | `/data`（BASE_DIR） | ✅ 挂载 `/data` 即持久化 |
| 面板配置 `.env` | `/data/.env`（工作目录） | ✅ 放挂载目录里即可 |
| 二进制 `system-panel` | `/tmp`（FILE_PATH） | ❌ 容器重建自动重新下载 |
| `cloudflared` | `/tmp`（FILE_PATH） | ❌ 同上 |

**持久化原理**：面板 `BASE_DIR` 默认取当前工作目录，`start.sh` 启动面板前 `cd /data`，所以面板数据自然落在挂载卷上。

**自定义数据目录**：可通过环境变量覆盖：
```bash
docker run -d \
  -v /宿主机目录:/mydata \
  -e BASE_DIR=/mydata \
  system-panel
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `8080` | 面板监听端口 |
| `PANEL_PASSWORD` | `123456` | 面板登录密码 |
| `BASE_DIR` | `/data` | 面板数据根目录（持久化卷挂载点） |
| `FILE_PATH` | `/tmp` | 二进制/依赖存放目录（不持久化） |
| `ENABLE_ARGO` | `false` | `true` 开启 Cloudflare Argo 隧道 |
| `ARGO_DOMAIN` | 空 | Argo 域名（配合 `ARGO_AUTH`） |
| `ARGO_AUTH` | 空 | Argo Token 或 TunnelSecret |
| `KEEPALIVE` | `false` | `true` 时容器内自动守护拉起面板进程 |
| `WS_ENABLE` | `false` | `true` 开启面板 WS 代理（订阅 `/sub`） |
| `UUID` | `7160b696-...` | 代理 UUID |
| `SUB_PATH` | `sub` | 订阅路径 |
| `SUB_NAME` | `test` | 订阅名称 |
| `CF_IP` | `ip.sb` | 优选 IP 查询 |
| `MY_DOMAIN` | 空 | 自定义域名（`ENABLE_ARGO=false` 时使用） |
| `LOCAL_DOMAIN` | 空 | 本地域名 |
| `CLIENT_TYPE` | `v2` | 客户端类型 |
| `STATIC_IP` | 空 | 固定 IP（warp 出口场景用，自动加 `[]`） |

### 开启 Argo 快速隧道（示例）

```bash
docker run -d \
  --name system-panel \
  -e ENABLE_ARGO=true \
  -e ARGO_AUTH='你的token' \
  -v /宿主机/持久化目录:/data \
  system-panel
```

## 进程保活（KEEPALIVE）

- `KEEPALIVE=false`（默认）：面板进程退出 → `start.sh` 结束 → 容器退出。配合 `--restart unless-stopped` 由 Docker 负责拉起。
- `KEEPALIVE=true`：容器内 `keep_alive` 循环每 55s 检查，面板/cloudflared 挂掉自动重启，容器常驻。

## 容器内健康检查

镜像内置 HEALTHCHECK，每 2 分钟探测 `http://localhost:${PORT}/`（面板根路径）。容器异常时 `docker ps` 会显示 `unhealthy`。

## 常见问题

### Q: 容器启动即退出？
先看日志：`docker logs system-panel`
- 若提示 `ERROR: Failed to download ... after 3 attempts` → 网络无法访问 GitHub，重试或配代理
- 若提示 `ERROR: .../system-panel not found after download` → 同上，下载失败

### Q: 数据怎么备份？
直接备份宿主机挂载目录即可：
```bash
tar -czf panel-data-backup.tar.gz /宿主机/持久化目录
```

### Q: 换了端口怎么访问？
```bash
docker run -d -p 9090:8080 -e PORT=8080 system-panel  # 容器内 8080，映射宿主机 9090
# 或
docker run -d -p 9090:9090 -e PORT=9090 system-panel  # 直接改容器内端口
```
（healthcheck 会跟随 `PORT` 环境变量自动适配）

## 构建说明

- 基础镜像 `alpine:latest`，已装 `bash wget curl grep procps unzip git nodejs python uv`
- 面板二进制启动时从 GitHub releases 下载（`kahunama/myfile`），首次启动需要外网
- 二进制下载到 `/tmp`（不持久化），镜像本身不含二进制，构建/拉取镜像体积小
