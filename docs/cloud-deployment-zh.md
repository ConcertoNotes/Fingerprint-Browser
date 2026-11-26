# 棱镜浏览器（Prism Browser）多租户云下发服务端 VPS 一键部署与客户交付指南

本指南面向**非专业运维人员**，手把手带你用一台低配云服务器（VPS）在 **10 分钟内**上线属于自己的「棱镜浏览器多租户云下发服务端」，并完成以下商业交付闭环：

- 在可视化中文管理后台（`https://cloud.你的域名.com/admin`）统一管理上游机场订阅池与客户独享静态 IP；
- 一客一授权码（`prism_...`），精准配置每位客户的**到期时间**、**最大设备数**、**节点分组与地区筛选**；
- 一条命令打出内置你云端网址的「客户发行版」macOS / Windows 安装包发给客户；
- 客户输入授权码即可自动拉取专属云节点（**全程隐藏上游链接、IP、端口与账密**），同时依然可以自由添加自己的本地节点。

---

## 1. 为什么需要一台 VPS？买什么配置最划算？

### 为什么不能只装在自己电脑上？

| 对比维度 | 装在自己本地电脑 | 部署在一台独立 VPS |
| --- | --- | --- |
| 在线时长 | 关机/休眠后客户无法激活、无法同步节点 | **7×24 小时稳定在线** |
| 公网 HTTPS 证书 | 家庭宽带通常无固定公网 IP 且封禁 80/443 端口 | **自动申请并续期免费 HTTPS 证书**（客户端强制 HTTPS 校验） |
| 权限回收与风控 | 停用客户/到期踢设备无法及时触达 | 随时手机/电脑打开 `/admin` 后台即时生效 |
| 上游订阅保护 | — | 上游机场链接仅由 VPS 服务端代拉，**绝不下发到客户电脑** |

### 推荐 VPS 配置

`prism-cloud-server` 基于轻量 Python + SQLite + Caddy，不承载代理流量转发（仅下发加密节点与策略），资源占用极低：

- **CPU / 内存**：**1 核 1 GB**（推荐 1 核 2 GB 更宽裕，足以轻松服务数百名客户）
- **硬盘**：20 GB SSD 即可
- **操作系统**：**Ubuntu 22.04 / 24.04 LTS** 或 **Debian 12**
- **线路建议**：优先选择能稳定访问你上游机场订阅链接且国内直连顺畅的地区（如香港、新加坡、日本、美国 CN2 等）
- **防火墙 / 安全组放行端口**：
  - `22/tcp`（SSH 远程登录）
  - `80/tcp`（Let's Encrypt 自动签发证书与 HTTP 跳转 HTTPS）
  - `443/tcp` 与 `443/udp`（HTTPS 正式服务）
  - **注意**：不要把 `8787` 端口暴露到公网！本套件已将 `8787` 严格限制在 Docker 内部网络中，仅由 Caddy 提供 HTTPS 反向代理。

---

## 2. 域名解析（A 记录）与安装 Docker

### 2.1 添加域名 A 记录

准备一个域名（例如 `yourdomain.com`），在域名服务商（Cloudflare / 阿里云 / 腾讯云 / Namecheap 等）的 DNS 解析页面添加一条 `A` 记录：

| 记录类型 | 主机记录（Name） | 记录值（IPv4） | 代理状态（若用 Cloudflare） |
| --- | --- | --- | --- |
| `A` | `cloud` | 你的 VPS 公网 IP（如 `203.0.113.10`） | 先选 **仅 DNS（灰色云朵）**，待证书签发成功 |

添加后在本机终端测试是否生效（看到返回你的 VPS IP 即成功）：

```bash
ping -c 2 cloud.yourdomain.com
```

### 2.2 登录 VPS 并安装 Docker

使用 SSH 登录你的 VPS：

```bash
ssh root@你的VPS公网IP
```

安装 Docker 与 Docker Compose 插件（Ubuntu / Debian 通用）：

```bash
apt-get update && apt-get install -y curl git ca-certificates
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker compose version
```

看到输出类似 `Docker Compose version v2.x.x` 即表示安装成功。

如果 VPS 开启了 `ufw` 防火墙，放行 `22 / 80 / 443` 端口：

```bash
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
```

---

## 3. 一条命令启动云服务端并自动签发 HTTPS 证书

### 3.1 获取代码并进入部署目录

```bash
git clone https://github.com/ConcertoNotes/Fingerprint-Browser.git /opt/prism-cloud
cd /opt/prism-cloud/cloud_server
```

### 3.2 生成环境变量文件 `.env`

复制模板并收紧文件权限：

```bash
cp .env.example .env
chmod 600 .env
```

先在终端生成两个高强度随机密钥（记下来备用）：

```bash
# 1) 生成管理员后台登录密码 (PRISM_CLOUD_ADMIN_TOKEN)
openssl rand -hex 24

# 2) 生成 44 位 Fernet 数据库加密主密钥 (PRISM_CLOUD_SECRET_KEY)
python3 -c "import os, base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

编辑 `.env`（可用 `nano .env`）：

```env
PRISM_CLOUD_DOMAIN=cloud.yourdomain.com
PRISM_CLOUD_ADMIN_TOKEN=把上面生成的管理员密码粘贴到这里
PRISM_CLOUD_SECRET_KEY=把上面生成的44位Fernet密钥粘贴到这里
```

> **提示**：多租户客户交付场景下，请保持 `PRISM_CLOUD_TOKEN` 为注释状态，避免开放共享通用客户端密钥；所有客户都应在后台使用各自的专属授权码。

### 3.3 复制 Caddy 配置并准备持久化数据目录

```bash
cp Caddyfile.example Caddyfile
mkdir -p cloud_data
chown -R 1000:1000 cloud_data
```

> **说明**：`Caddyfile.example` 默认会自动读取 `.env` 中的 `PRISM_CLOUD_DOMAIN`，无需重复修改；容器内部以非 root 用户（UID `1000`）运行，执行 `chown -R 1000:1000 cloud_data` 可确保 SQLite 数据库写入权限正常。

### 3.4 一键启动并验证 HTTPS

```bash
docker compose up -d --build
```

等待约 20–60 秒构建并自动申请证书后，验证运行状态：

```bash
# 查看容器均为 healthy / Up
docker compose ps

# 验证公网 HTTPS 与健康检查
curl https://cloud.yourdomain.com/health
```

看到返回以下 JSON 即表示部署彻底成功：

```json
{"status":"ok","service":"prism-cloud-server","protocol_version":1}
```

若证书尚未签发成功，可查看 Caddy 日志排查（最常见原因是域名 A 记录未生效或云厂商安全组未放行 `80/443`）：

```bash
docker compose logs -f caddy
```

---

## 4. 登录可视化管理员后台 (`/admin`) 管理节点与客户

在电脑或手机浏览器中打开：

```text
https://cloud.yourdomain.com/admin
```

输入你在 `.env` 中设置的 `PRISM_CLOUD_ADMIN_TOKEN` 即可登录。建议按以下 4 步完成首批客户配置：

### 第一步：添加「上游节点池」（共享机场 / 公共静态 IP）

1. 切换到 **「上游节点池」** 页面，点击 **「新增上游源」**；
2. 选择类型：
   - **订阅链接（URL）**：粘贴上游机场的 Clash/Mihomo/通用订阅链接；
   - **订阅文本（YAML / Base64）**：粘贴完整配置文本；
   - **手动静态 IP（Manual）**：按行粘贴 `socks5://user:pass@ip:port#备注` 或 `ip:port:user:pass`；
3. 保存后点击 **「立即拉取测试」**，服务端会在 VPS 内部完成抓取、SSRF 校验与清洗，并在后台展示解析出的节点总数与预览列表。
4. **防泄密保障**：上游链接以 Fernet 密文存储在服务端数据库，**永远不会发送给任何客户电脑**。

### 第二步：新建客户并复制专属授权码

1. 切换到 **「客户管理」** 页面，点击 **「新建客户」**；
2. 填写：
   - **客户名称 / 备注**（如 `跨境团队-王总`）；
   - **到期时间**：可直接点击快捷按钮（`+7 天` / `+30 天` / `+90 天` / `+1 年` / `永不过期`）或自定义具体日期；
   - **设备数上限**：默认 `3` 台（`0` 表示不限制）；
   - **离线宽限期**：默认 `24` 小时（客户断网超过该时长或人为回调系统时间时，云节点自动暂停直到重新联网验证）；
   - **云端同步浏览器配置（指纹/Cookie）**：默认**关闭**，仅当客户需要多电脑同步浏览器环境时再单独开启（数据严格按客户隔离）；
3. 点击创建后，弹窗会**一次性展示**该客户的专属授权码（格式如 `prism_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`）。请立刻点击 **「复制」** 发给客户（数据库仅保存 SHA-256 哈希，关闭弹窗后无法再次查看明文，如遗忘可随时一键「重置授权码」）。

### 第三步：为客户分配节点

在客户列表右侧点击 **「分配节点」** 打开抽屉：

- **方式 A（从共享节点池分配）**：勾选一个或多个共享源；可以填写自定义展示分组名，以及 **节点包含/排除关键词**（例如填 `美国`、`洛杉矶`，即可把同一个大机场按地区拆分给不同客户）并实时预览命中节点数；
- **方式 B（粘贴客户专属独享静态 IP）**：在抽屉下半区直接粘贴该客户独享的静态 IP（支持 `IP:端口:账号:密码`、`socks5://...`、常见代理分享链接），一键保存立即生效，**其他客户完全不可见**。

### 第四步：（可选）查看全局隔离防检测策略

在 **「隔离策略」** 页面可以查看当前推送给所有客户端的统一防检测与防泄露策略版本，后续升级规则只需在后台一键发布，所有客户下次启动或 15 分钟静默心跳时自动生效。

---

## 5. 一条命令打包「客户发行版」安装包发给客户

你**不需要为每个客户单独打包**！只需用自己的云服务域名打出一份通用的「客户发行版」安装包，然后把**同一份安装包 + 每位客户各自的专属授权码**发给他们即可。

### 客户发行版与标准版的区别

- 自动内置并锁定你的云端网址（客户看不到也无法修改服务器地址）；
- 侧边栏「云下发中心」精简为直观的 **「我的授权」**，首页未激活时自动弹出激活码引导框；
- 屏蔽主窗口开发者工具（DevTools）与本地 `/docs` 接口文档；
- 云下发节点隐藏入口 IP、端口、协议与账密且禁止编辑删除；
- **完整保留客户自由添加本地机场订阅与本地静态 IP 的能力**。

### 打包 macOS 客户发行版（在 macOS 上执行）

```bash
uv run python scripts/build_desktop.py \
  --package \
  --edition customer \
  --cloud-url https://cloud.yourdomain.com \
  --brand-name "棱镜浏览器"
```

打包完成后，安装包位于：
- `dist/prism-browser-customer-macos-arm64.dmg`（Apple Silicon 芯片）或 `x64.dmg`（Intel 芯片）
- 同名 `.sha256` 校验文件

### 打包 Windows 客户发行版（在 Windows PowerShell 上执行）

```powershell
uv run python scripts/build_desktop.py `
  --package `
  --edition customer `
  --cloud-url https://cloud.yourdomain.com `
  --brand-name "棱镜浏览器"
```

打包完成后，安装包位于：
- `dist/prism-browser-customer-windows-x64-setup.exe`
- 同名 `.sha256` 校验文件

### 常用可选参数

| 参数 | 说明 |
| --- | --- |
| `--brand-name "你的品牌名"` | 自定义窗口标题、macOS 菜单名与 Windows 快捷方式名称 |
| `--require-activation` | 开启强激活提示（未输入有效授权码前不显示「暂不激活」关闭按钮） |
| `--package-only` | 已有 `build/prism-backend/` 时跳过 WebUI 与 Python 打包，快速生成新配置安装包 |
| `--skip-webui` / `--skip-runtime` | 复用本地已构建好的前端或离线运行时以加快打包速度 |
| `--allow-insecure-dev-url` | **仅限开发者本机联调**允许 `http://` 或 `localhost`，正式发给客户严禁使用 |

---

## 6. 客户收到安装包后的 30 秒使用流程

你可以把下面这段简短说明直接转发给客户：

1. **安装并打开软件**：双击 `.dmg`（macOS）或 `-setup.exe`（Windows）完成安装并启动；
2. **输入授权码激活**：在弹出的「激活授权」窗口（或左侧 **「我的授权」**）中粘贴管理员提供的专属授权码（`prism_...`），点击 **「立即激活并同步节点」**；
3. **绑定节点开浏览器**：进入 **「订阅与节点」** 或 **「浏览器环境」**，即可直接选用已自动同步好的专属加密云节点（页面可查看地区与延迟，并支持一键测速与创建隔离环境）；
4. **随时自由添加本地节点**：如果客户自己也有其他静态 IP 或机场订阅，可随时在 **「订阅与节点」** 中点击添加本地静态 IP / 本地订阅。本地节点与云端节点完全独立——即使云授权到期，客户自己添加的本地节点与浏览器数据也完全不受影响。

---

## 7. 日常运维与安全边界说明

### 7.1 常见日常操作（全部在 `/admin` 后台点击完成）

- **客户续期**：在客户行点击「续期」或修改到期日期 → 客户在客户端点击「同步」或等待下一次 15 分钟后台心跳即可自动恢复云节点，原有浏览器绑定自动保留；
- **临时停用 / 恢复**：点击「停用」后，客户端在下次同步/心跳（最长 15 分钟内）会自动断开所有正在使用云节点的浏览器窗口并清空本地云节点凭据；
- **踢出多余设备**：点击「设备（如 `3/3`）」查看已绑定设备名、版本与最近活跃时间，点击「踢下线」立即吊销该设备并释放名额；
- **重置授权码**：客户疑似泄露授权码时，点击「重置授权码」，旧授权码立即作废、租户加密盐 `seal_salt` 自动轮换，将新授权码发给客户重新激活即可；
- **更换或刷新上游机场**：在「上游节点池」编辑链接并点击「立即拉取」，所有关联客户会自动同步到最新节点；
- **查看审计日志**：在「审计日志」页可追踪管理员操作及客户到期/超设备数拦截记录。

### 7.2 数据备份、恢复与版本升级

**务必定期备份两样东西**：
1. `cloud_server/cloud_data/cloud_server.db`（客户、节点池与审计数据库）；
2. `cloud_server/.env`（尤其是其中的 `PRISM_CLOUD_SECRET_KEY`，丢了它无法解密数据库里已加密的上游源！）。

推荐在 VPS 上使用 SQLite 在线热备份（不锁服务、不损数据）：

```bash
cd /opt/prism-cloud/cloud_server
mkdir -p backups
docker compose exec -T cloud-server python -c \
  "import sqlite3; src=sqlite3.connect('/data/cloud_server.db'); dst=sqlite3.connect('/data/backup.db'); src.backup(dst); dst.close(); src.close()"
mv cloud_data/backup.db "backups/cloud_server-$(date +%F).db"
cp .env "backups/.env.$(date +%F).bak" && chmod 600 "backups/.env.$(date +%F).bak"
```

**版本升级**：

```bash
cd /opt/prism-cloud
git pull
cd cloud_server
docker compose up -d --build
```

### 7.3 防节点账密外泄的安全边界说明

为避免预期偏差，请充分了解本方案已经提供的防护能力与客观物理边界：

- **已实现的客户端全链路防外泄加固**（可拦截绝大多数普通客户与半技术用户窃取节点）：
  1. 上游机场订阅 URL 仅在 VPS 服务端解析，**永不下发给客户端**；
  2. 客户端界面、本地 REST API、SQLite 展示列与错误日志**完全隐藏**上游入口 IP、端口、协议参数与账密，并禁止通过 API 修改或导出云节点；
  3. 代理核心（Mihomo）加载完配置就绪后，磁盘上的运行期 `config.yaml` **立即覆写擦除**，日志对上游 `server` / `sni` / 账密全字段脱敏；
  4. 本地主密钥在 macOS/Windows 上由系统钥匙串（Electron `safeStorage`）加密保护，云节点配置使用 `HKDF(本地主密钥, 租户 seal_salt)` 双层密封（`enc2:`），直接拷贝硬盘数据库无法离线解密；
  5. 客户发行版锁定云端网址，并屏蔽主壳 DevTools 与本地 `/docs` 接口文档。
- **客观物理边界**：
  - 只要代理流量是由客户电脑上的本地代理内核直接拨号连接上游代理 IP，拥有本机系统管理员权限且具备逆向能力的极端攻击者，理论上仍可能通过底层网络抓包（如 Wireshark）或转储运行中代理进程的内存观察到目标代理 IP/端口。
  - 若未来业务对少数超高敏节点有 **100% 绝对零暴露上游 IP** 的要求，唯一终极方案是在二期增加**云端转发网关（Relay Mode）**（客户端只连你的中转服务器，再由中转服务器二次转发至上游节点）。当前多租户架构已为此预留扩展位。
