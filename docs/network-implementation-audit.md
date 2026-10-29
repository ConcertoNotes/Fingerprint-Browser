# 网络节点 / 地图代理改造：第 0 阶段仓库审计

审计日期：2026-10-29。本文件记录改动前的真实基线、现有实现与扩展点，是后续阶段的依据。
所有结论均来自当前工作树源码、本地 OpenAPI 输出与本机命令结果；未验证的内容明确标注。

依据的需求文档：仓库根目录未跟踪的 `camoufox.md`（开发文档 V1.1）与 `claude_code.md`（执行提示词）。
两份文件的实际文件名与提示中的《Camoufox_指纹浏览器_地图代理改造开发文档.md》等不同，内容一致。

## 1. 基线

| 项目 | 值 |
| --- | --- |
| HEAD | `ea62ee19ceeeb193554b88456e7b739270bafd7e`（`docs: refresh delivery status…(#146)`） |
| 分支 | 审计在 `main` 上完成，随后创建 `feat/network-node-map`（尚无提交） |
| 工作树 | 干净；仅有未跟踪的 `camoufox.md`、`claude_code.md` |
| remote | `origin = https://github.com/polyackiy/camoufox-profile-manager.git` —— **是上游仓库，不是个人 Fork**。推送前需要改为自己的 Fork |
| 软件版本 | `pyproject.toml` 0.6.0；README 说明 `main` 含未发布的 0.7.0 改动 |
| 主机 | macOS 27.0.1（26A434），arm64（Apple Silicon） |
| Python | 3.12.15（uv 管理，与 CI 主版本一致；CI 矩阵 3.10–3.13）；系统 `/usr/bin/python3` 为 3.9.6，不满足 `>=3.10`，不使用 |
| uv | 0.13.5 |
| Node / npm | v26.12.1 / 11.20.0（CI 使用 20 与 22；本机更新，构建通过） |
| 关键 Python 依赖（uv.lock） | camoufox 0.5.8、playwright 1.62.0、fastapi 0.141.1、pydantic 2.13.4、httpx 0.28.1、httpcore 1.0.9（固定）、cryptography 50.0.2、pyyaml 6.0.3（**仅随 `chrome-migration` extra 安装**）、pywebview 6.2.1、ruff 0.16.1、mypy 2.3.0、pytest 9.1.1、SQLite 3.53.1 |
| 关键前端依赖 | next 16.4.3、react / react-dom 19.3.0、typescript 6.0.3、tailwindcss 4、lucide-react；**没有任何地图库** |
| Camoufox 浏览器 | 审计开始时未安装；经确认后用 `uv run camoufox fetch` 安装 **156.0.1-beta.36**（mac.arm64，1.3 GB），与 wrapper 的 `browser-pin.json` 一致，`ensure_browser_ready()` 与 `browser_resources_ready()` 通过 |
| GeoIP 数据库 | 同次 fetch 安装 **GeoIP AIO by daijro**（`~/Library/Caches/camoufox/geoip/mmdb/geoip aio by daijro-combined.mmdb`，115 MB，`database_type=GeoIP2-City`，IPv4/IPv6 合并） |
| `mihomo` 可执行文件 | **未安装**（`which mihomo` 无结果）；第 2 阶段前再按需准备 |
| `GeoLite2-City.mmdb` | 本机没有，也不需要：Camoufox 当前默认数据源是 AIO |
| 系统代理 | **本机开启了系统代理** `127.0.0.1:7890`（HTTP/HTTPS/SOCKS，`scutil --proxy`），并设置了 `HTTP_PROXY`/`HTTPS_PROXY=http://127.0.0.1:7890`、`NO_PROXY=localhost,127.0.0.1,::1,.local`。7890 端口已被用户自己的代理客户端占用。见第 5.6 节 |
| NSS `certutil` | 未安装（浏览器 TLS 代理验收测试因此跳过） |

安装命令（与 CI 一致的 lock）：

```bash
uv sync --locked --extra dev --extra desktop --extra chrome-migration --extra updates --extra sync --python 3.12
cd web && npm ci
```

`npm ci` 提示 esbuild / fsevents / unrs-resolver 的 install script 未被 `allowScripts` 允许；
本次 `npm test`、lint 与构建仍然通过，暂不处理。

## 2. 基线测试结果

| 命令 | 结果 |
| --- | --- |
| `uv run ruff check src tests scripts/configure_ci_apt.py` | 通过（All checks passed） |
| `uv run ruff format --check src tests scripts/configure_ci_apt.py` | 通过（278 files already formatted） |
| `uv run mypy src/camoufox_pm` | **1 个错误**：`desktop_recovery.py:156` `Window \| None` 赋给 `None` 类型变量。原因是本机安装了 `desktop` extra，pywebview 6.2.1 自带 `py.typed`；CI 后端任务不安装该 extra，因此 CI 不会报错。这是环境相关的既有问题，不是本次改造引入的；按“不做无关重构”原则不在本阶段修改，记为已知项 |
| `uv run pytest -m "not browser" -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning -o faulthandler_timeout=300` | **2401 passed, 8 skipped, 48 deselected**，68 s。跳过项均为仅限 Windows 原生 ACL/DACL 的测试 |
| `cd web && npm test` | 25/25 通过 |
| `cd web && npm run lint` | 通过 |
| `cd web && npx tsc --noEmit` | 通过 |
| `uv run python scripts/build_webui.py` | 成功，产物写入 `src/camoufox_pm/webui/`（git-ignored） |
| `uv run pytest -m browser --no-network` | **34 passed, 13 skipped, 1 failed**，420 s。跳过：11 个 `test_proxy_protocols.py`（未装 NSS certutil）、1 个仅限 Windows 新机诊断、1 个仅限 CI 一次性缓存。失败项见下 |
| 两个 Profile 同时运行（开发文档 D1 验收） | **通过**，见下 |

**唯一失败**：`tests/browser/test_cookie_transfer.py::test_cookie_import_is_sent_with_scope_flags_and_sessions_end_on_close`，
`Page.goto: NS_ERROR_NET_ERROR_RESPONSE`（访问 `http://cookie.test:<port>/`）。判定为**本机环境原因，不是代码回归**：

- 该测试通过 Firefox 偏好 `network.dns.localDomains` 把合成域名 `cookie.test` 指向回环；Profile 没有设置代理时，
  Firefox 默认跟随操作系统代理，于是请求被发到 `127.0.0.1:7890`；
- 清除 `HTTP(S)_PROXY` 环境变量后重跑仍然失败，说明是**操作系统级**代理而非环境变量；
- 直接验证：`curl -x http://127.0.0.1:7890 http://cookie.test:49871/` 返回 **HTTP 502**，与 Firefox 的错误一致；
- 同文件中只用 `localhost` 的测试通过（在系统代理例外中）。CI 运行环境没有系统代理。
- 未修改系统代理设置（属于系统设置，不应由本流程修改）。如需最终确认，可在临时关闭系统代理后单独重跑此测试。

**双 Profile 基线**（脚本通过真实 FastAPI lifespan + `TestClient` 调用 `/api/v1`，临时数据库与 `CPM_SECRET_KEY`，有界面启动，未设代理）：

- 两个新 Profile 都返回 `launched`，`/api/v1/browsers/active` 同时显示 2 个；
- 对 A 再次 launch 返回 `already_running`，没有产生第二个实例；
- 两者 `storage_path` 不同（`profiles/profile_<id>`），运行期间都持有 lease，首次启动都写入了固定指纹；
- 关闭后都返回 `closed`，active 为空，lease 全部释放，数据目录保留，未发现残留浏览器进程；
- 注意：macOS 上会话的 `process_id` 为 `null`（取不到驱动 pid），因此 pid 轮询兜底**不生效**，会话清理完全依赖
  Playwright 的 `close`/`disconnected` 事件。Mihomo 子进程清理不能依赖浏览器 pid，必须由会话 teardown 和自身的进程句柄负责；
- 由于未设代理且本机有系统代理，这两个“直连” Profile 实际经由用户的系统代理出网（见第 5.6 节）。

结论：现有代码无初始回归；唯一失败的浏览器测试由本机系统代理导致。

## 3. OpenAPI 与路由

本地导入 `camoufox_pm.main:app` 生成 OpenAPI：110 个路径。与本次相关、**已存在且不得覆盖**的接口：

```text
GET        /api/v1/browsers/active
POST       /api/v1/browsers/close-all
POST       /api/v1/profiles/clear-geography
POST       /api/v1/profiles/{profile_id}/check-proxy
POST       /api/v1/profiles/{profile_id}/close
POST       /api/v1/profiles/{profile_id}/launch
GET,POST   /api/v1/proxies
POST       /api/v1/proxies/assign-sequence
POST       /api/v1/proxies/assignment-preview
POST       /api/v1/proxies/import
PATCH,DEL  /api/v1/proxies/{proxy_id}
POST       /api/v1/proxies/{proxy_id}/assign
POST       /api/v1/proxies/{proxy_id}/check
POST       /api/v1/proxy/check
```

`/api/v1/network/*` 下没有任何路径，可以新增，无冲突。

路由注册方式（`src/camoufox_pm/main.py:184` 起）：每个 router 同时挂在 `/api/v1`（进入 schema）与 `/api`（兼容别名，不进 schema），
统一带 `dependencies=[Depends(require_auth)]`。新 `network` router 应按同样方式加入循环。

### API 约定（与开发文档的差异）

- **响应包装**：资源类端点（如 `GET /profiles/{id}`）直接返回资源；动作/系统端点使用
  `ApiResponse{success,message,data}`（`api/models/system.py:10`）。代理库 `/proxies` **全部**使用 `ApiResponse`。
  新 `/network/*` 端点沿用代理库做法，统一使用 `ApiResponse`。
- **错误体**：所有非 2xx 统一为 `{"error":{"code","message","details"},"detail"}`（`api/errors.py`）。
  `409` 的 code：`stale_write`（版本过期）或 `conflict`（运行中/被 lease）。开发文档提出的 `profile_running`
  不是现有 code；建议绑定接口在 Profile 运行时沿用现有 `ProfileLocked → 409 conflict` 处理器，不另造新 code。
- **权限**：`require_admin()`（非管理员 `403`）；`require_profile_access(id, "view"|"edit"|"launch"|"export")`，
  无权访问时返回 **`404`（与不存在相同）而不是 `403`**。代理库所有操作都要求管理员。
  建议：订阅/节点/检测 = `require_admin`（节点含共享密钥，同代理库）；绑定 = 管理员，或至少 `require_profile_access(id, "edit")`
  再加节点可见性检查——第 3 阶段确定。
- **版本控制**：`row_version BIGINT NOT NULL DEFAULT 0`，写入用 `WHERE id=? AND row_version=?`，未命中抛 `StaleWriteError`。
  开发文档的 `profile_network_bindings.row_version DEFAULT 1` 应改为与项目一致的 `DEFAULT 0`。
- 敏感/动作响应设置 `Cache-Control: no-store`（见 `proxies.py` 的 assignment 路由）。

## 4. 数据库与迁移体系

- 单一 SQLite 文件（CLI 默认 `data/profiles.db`，桌面端在系统数据目录），`DatabaseManager`（`core/database.py:63`）持有一个长连接，
  启动时设置 `PRAGMA foreign_keys = ON`、`journal_mode = WAL`。
- 启动流程：`check_schema` → `_create_tables`（`CREATE TABLE IF NOT EXISTS`）→ `_migrate`（缺列补列）→ `_create_indexes`
  → `complete_migration`（`core/schema_migrations.py`）。
- **编号迁移**：`schema_migrations.py` 中 `CURRENT_SCHEMA = 5`，用 `PRAGMA user_version` 记录；每个新版本在
  `BEGIN IMMEDIATE` 事务中执行，并可写 `manager_features(feature, version)`，使**更老的管理器拒绝打开新库**
  （`check_schema` 遇到更高 `user_version` 直接报错）。
- 功能模块自带表定义的模式：`extension_library` / `profile_templates` / `saved_filters` / `template_batches`
  通过各自的 `initialize_tables(connection)` 在 `_create_tables` 中建表。
- `update_validation.py:83` 要求更新包 manifest 的 `schema_result == CURRENT_SCHEMA`，提升版本号会自动参与更新校验。

### 现有相关表（真实结构）

```sql
CREATE TABLE profiles (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, group_id TEXT, status TEXT DEFAULT 'active',
  browser_settings TEXT NOT NULL, proxy_config TEXT, extensions TEXT,
  tags TEXT NOT NULL DEFAULT '[]', storage_path TEXT, notes TEXT,
  created_at TIMESTAMP, updated_at TIMESTAMP, last_used TIMESTAMP,
  fingerprint TEXT, proxy_check TEXT,
  locked_by TEXT, lock_expires TIMESTAMP,          -- lease
  row_version BIGINT NOT NULL DEFAULT 0, deleted_at TIMESTAMP  -- 乐观锁 / 回收站
);
CREATE TABLE proxy_inventory (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, server TEXT NOT NULL,
  username TEXT, password TEXT,  -- Fernet 'enc:' 前缀密文
  country TEXT, row_version BIGINT NOT NULL DEFAULT 0, last_check TEXT,
  created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL
);
```

- `profiles.id` 确为 `TEXT`（8 位短 ID），物理表名就是 `profiles`，开发文档中的外键 `REFERENCES profiles(id)` 成立。
- 已有表使用 `REFERENCES … ON DELETE CASCADE` 的先例：`profile_grants`、`sessions`、`workflow_runs`。
- 删除 Profile 分两步：`DELETE /profiles/{id}` 只设 `deleted_at`（进回收站，行仍在）；回收站永久删除
  `permanently_delete_profile` 才执行 `DELETE FROM profiles`。因此 `profile_network_bindings` 的
  `ON DELETE CASCADE` 只在永久删除时触发，回收站中的 Profile 会保留绑定（恢复后仍有效）。
- Profile 保存使用 `INSERT … ON CONFLICT(id) DO UPDATE`，**不是** `INSERT OR REPLACE`，不会因为删除再插入而触发级联删除绑定。

### 新表落地方式（第 1 阶段）

在 `schema_migrations.py` 增加 **迁移 6**（`CURRENT_SCHEMA = 6`）：同一 `BEGIN IMMEDIATE` 事务内创建
`network_subscriptions`、`network_nodes`、`network_node_checks`、`profile_network_bindings` 与索引，并写入
`manager_features('network-nodes', 1)`。表结构以开发文档第 6 节为基础，做以下对齐：

- 时间列沿用项目 ISO-8601 UTC 文本；`row_version` 默认 0；`revision` 名称保留在订阅表。
- `network_nodes(subscription_id, node_key)` 唯一索引：SQLite 中 `NULL` 互不相等，手动节点
  （`subscription_id IS NULL`）需要额外的部分唯一索引 `ON network_nodes(node_key) WHERE subscription_id IS NULL`。
- `profile_network_bindings.node_id … ON DELETE RESTRICT` 与“节点只退役不删除”的策略一致。
- **代价**：迁移 6 之后，旧版本管理器（含已发布 0.6.0）会拒绝打开该数据库。这与项目已有迁移 3/4/5 的策略相同，
  需要在发布说明中写明；迁移前由现有更新/备份机制保留数据库副本。

## 5. 现有代理体系

### 5.1 代理模型与存储

- `ProxyConfig`（`core/models.py:85`）：`type ∈ {http, https, socks4, socks5}`（`socks4a`/`socks5h` 为别名）、
  `server`（`host:port`）、`username`、`password`、`country`。`to_camoufox_format()` 生成 Playwright 代理字典。
- Profile 上的代理存于 `profiles.proxy_config`（JSON，密码经 `_serialize_proxy` 加密）。
- 代理库 `proxy_inventory`（`core/proxy_inventory.py`）：列表查询不读取密码，仅返回 `has_password`；
  密码写入通过 `_stored_password`，**未配置密钥时直接拒绝**（`ProxyCredentialsUnavailable`）；
  解密失败（`enc:` 原样返回）同样拒绝。
- 加密层 `core/crypto.py`：Fernet，`enc:` 前缀；**未配置 `CPM_SECRET_KEY` 时 `encrypt` 是恒等变换**。
  桌面模式会自动生成 `secret.key`（`config.py:99`）。新节点/订阅密钥必须复用 `_stored_password`/`_clear_password`
  式的“无密钥即拒绝”包装，不能直接调用 `encrypt`。
- 传输错误信息的脱敏：`_sanitize_check` 会把用户名/密码（含 URL 编码形式）替换为 `[redacted]`。

### 5.2 代理“绑定”方式

`POST /proxies/{id}/assign` 在 `profile_operation`（跨进程 lease + admission 锁）内，用一条带
`row_version`、`locked_by` 与库条目 `row_version` 条件的 `UPDATE profiles SET proxy_config=…` 把配置**复制**进 Profile。
库条目与 Profile 之间没有引用关系。

### 5.3 与 `profile_network_bindings` 协同（无外键冲突）

- 新表只引用 `profiles(id)` 与 `network_nodes(id)`，与 `proxy_inventory` 无外键关系，不会冲突。
- 启动时决策（第 2 阶段在 `NetworkAdapter.prepare` 中实现）：存在 `profile_network_bindings` 行 → 使用绑定节点；
  否则 → 回退到 `profiles.proxy_config` 原逻辑，旧调用行为不变。
- 若 Profile 同时有 `proxy_config` 与绑定，绑定优先；UI 必须明确显示“当前生效的是节点绑定”，避免用户误以为手动代理生效。
- 原 `proxy_inventory` 条目若要出现在地图上，可选地映射为 `subscription_id IS NULL` 的 `network_nodes` 行（V1 可不做）。

### 5.4 出口检测（可直接复用）

`core/proxy_check.py`：

- `resolve_exit_ip(proxy)`：通过代理用 httpx 访问 `api.ipify.org` / `checkip.amazonaws.com` / `icanhazip.com`，
  TLS 校验、响应体 ≤256 字节、拒绝压缩、整端点超时，返回 `(ip, latency_ms)`；SOCKS4 走临时中继。
- `locate(ip)`：调用 `camoufox.geolocation.get_geolocation`，返回 `country / timezone / latitude / longitude`，
  **不含城市名**（Camoufox 的 `Geolocation` 数据类没有 city 字段）。
- `prepare_proxy_geoip(proxy, options)`：启动前通过上游代理解析新鲜出口 IP，把字面 IP 传给 Camoufox 的 `geoip`；
  失败直接阻止启动（已有 fail-closed 行为）。
- `fill_what_geoip_would_have`：设置了坐标时补齐时区与 WebRTC 地址。

对新功能的意义：Mihomo 会话的本机入口可以表示为 `ProxyConfig(type=http, server="127.0.0.1:<port>")`，
直接交给 `resolve_exit_ip` / `prepare_proxy_geoip`，即“经过节点本身测出口”，无需另写探测客户端；
测的是出口而不是入口 host。

### 5.5 GeoIP 数据的真实情况（与开发文档的差异）

- Camoufox 0.5.8 默认 GeoIP 源是 **“GeoIP AIO by daijro”**（`geoip-aio-all.mmdb`，合并 IPv4/IPv6），
  不是 `GeoLite2-City.mmdb`；也可切换为 GeoLite2 源。存放于 `~/Library/Caches/camoufox/geoip/mmdb/`。
- `get_geolocation` 在数据库缺失或超过一周时会**自动联网下载**。批量检测时需要先确认数据库就绪，避免检测过程触发下载。
- 城市名与 `precision=city`：已用 `maxminddb` 直接读取 AIO 库，抽查 8.8.8.8、1.1.1.1、133.242.0.1、81.2.69.142、
  2001:4860:4860::8888，**记录只有 `country.iso_code` 和 `location{latitude, longitude, time_zone}`**，
  没有 `city`、`subdivisions`，也没有 `accuracy_radius`。因此：
  - 用当前数据源**无法给出城市名**，也不能标 `precision=city`；
  - 有坐标时可以落点，但只能标“数据库坐标（近似）”，同一国家的不同 IP 常给出相同坐标（如 JP → 东京附近），
    同坐标多节点列表（开发文档 4.2）因此是必需的，而不是边缘情况；
  - 若要城市名，需要用户另行选择 GeoLite2 数据源（Camoufox 支持 `camoufox set --geoip`），并核实其许可证；
    建议 `precision` 取值改为 `unknown | country | coordinates`，城市名作为可选字段，只有数据源确实提供时才显示。
- 单元测试必须 Mock `locate`/mmdb；仓库已有 `--no-network` pytest 选项可阻断非回环连接（`tests/conftest.py`）。

### 5.6 本机系统代理带来的约束（审计中实测发现）

1. **未设代理的 Profile 会跟随操作系统代理**。Firefox 默认 `network.proxy.type=5`（使用系统设置），Camoufox 与本项目都没有改它。
   所以“Profile 无代理 = 直连”这一假设在开着系统代理的电脑上不成立。设置了显式代理的 Profile（包括以后的 Mihomo 本机入口）
   不受影响，但第 2 阶段必须用故障注入测试确认：显式代理失效时，Firefox **不会**退回系统代理或直连。
2. **httpx 默认读取 `HTTP(S)_PROXY` 环境变量**（`trust_env=True`）。`proxy_check.resolve_exit_ip` 在有代理时显式传入代理，不受影响；
   但 `proxy=None` 的直连检测、以及以后的订阅下载，会被环境变量里的代理接管。订阅下载必须显式决定走向
   （建议 `trust_env=False`，否则 SSRF 地址校验对实际连接无效），并在测试中覆盖。
3. **7890 端口已被用户自己的代理客户端占用**，而且该客户端很可能就是 Clash/Mihomo。Mihomo 端口分配必须以“绑定成功并就绪”为准，
   不能假设常见端口空闲；孤儿进程回收**绝不能**按进程名 `mihomo` 匹配，只能回收本程序登记的 pid + 私有配置路径。
4. 浏览器测试中依赖合成域名的用例在这类机器上会失败；以后新增的浏览器测试应显式设置代理或 `network.proxy.type`，
   不能默认依赖“无系统代理”。

## 6. 浏览器启动 / 关闭路径

调用链：`POST /profiles/{id}/launch`（`api/routes/profiles.py:396`）→ `require_profile_access(id,"launch")`
→ `ProfileManager.launch_browser`（`core/profile_manager.py:887`）。

1. `profile_admission`：跨进程文件锁，串行化同一 Profile 的启动/编辑/同步准入。
2. `_launch_browser`：
   1. `ensure_browser_ready()`；
   2. `storage.acquire_lease(profile_id, holder, CPM_LEASE_TTL)`——**lease 是“是否在运行”的权威**；失败 → `ProfileLocked`（409）或 404；
   3. `guard_profile`（同步保护）；
   4. 已在本进程运行 → 返回 `already_running`（不释放 lease）；
   5. `profile.to_camoufox_launch_options()`（`proxy` 来自 `profile.proxy.to_camoufox_format()`，`geoip = not geolocation`）；
   6. 固定指纹：首次 `fingerprint_store.resolve()` 并保存，之后回放；只写 `fingerprint`/`last_used` 两列；
   7. **`prepare_proxy_geoip(profile.proxy, options)`、`fill_what_geoip_would_have(...)`**；
   8. `browser_sessions.launch(profile_id, options, on_exit=…)`；
   9. 任意异常（含 `CancelledError`）：若该 Profile 不再 `is_live`，释放 lease 后重新抛出。
3. `BrowserSessionManager.launch`（`core/browser_session.py:368`）：
   - SOCKS4 或带认证 SOCKS5 → 创建会话独占的 `SocksRelay`（仅 `127.0.0.1`），把 `options["proxy"]` 换成本机中继；
   - SOCKS 强制 `network.proxy.socks_remote_dns`；
   - `AsyncCamoufox(**options).start()`；失败时构造 `BrowserSession` 执行 `terminate()` 清理中继与浏览器；
   - 成功后注册 Playwright `close`/`disconnected` 事件与 pid 轮询兜底。
4. 关闭：`close_browser` → `close_and_release` → `BrowserSession._terminate()`：先关 `socks_relay`，
   再 `camoufox.__aexit__`，再终止驱动进程，最后释放 lease。
5. lease 心跳：`BrowserSessionManager` 每 30 s 续租；续租失败（被别的实例抢走）即关闭本地浏览器。
   进程退出时 lifespan 调用 `release_all_leases`。

### 扩展点（第 2 阶段）

- **`SocksRelay` 是现成的先例**：会话独占的本机网络资源，由 `BrowserSession` 持有、在 `_terminate` 中关闭、
  启动失败时同样被清理。Mihomo 子进程应以同样方式挂到 `BrowserSession`（新增可选字段，如 `network_runtime`），
  而不是另建会话管理。
- 准备网络的位置：在 `_launch_browser` 中、**lease 获取之后**、`prepare_proxy_geoip` 之前。顺序天然满足开发文档
  “先 lease → 再准备网络 → 再启动浏览器”。Mihomo 准备失败时走已有异常分支释放 lease，并且必须先关闭已启动的 Mihomo。
- 把 Mihomo 本机端点作为 `ProxyConfig(http, 127.0.0.1:port)` 传给 `prepare_proxy_geoip`：出口查不到即阻止启动，复用现有 fail-closed。
- `already_running` 分支、lease 冲突、`is_live` 判断全部保持不变，满足“同一 Profile 只能一个活动实例”。
- 运行状态 `GET /network/runtime/{id}` 只能反映**本实例**的会话（`active_sessions` 仅本进程可见），文档需写明。

## 7. 是否已有订阅 / Mihomo / 地图功能

全仓（`src`、`web/src`、`docs`、`tests`、`pyproject.toml`）搜索 `mihomo|clash|subscription|maplibre|leaflet|geojson|
proxy-provider|vmess|vless|trojan|shadowsocks`：**无任何实现**。不存在订阅导入、复杂协议代理、Mihomo 进程管理或地图页面，
第 1–4 阶段均为新增，不会与现有功能重复。

可复用而非重写的部分：代理库（加密/脱敏/版本控制模式）、`proxy_check`（出口与 GeoIP）、`SocksRelay` 生命周期模式、
lease / `profile_operation` / `profile_admission`、`ApiResponse` 与错误处理、`clear-geography`、`update_permissions` 中的
`create_private_directory` / `create_private_file` / `verify_private_path`（Mihomo 私有临时目录 0700、配置 0600 可直接复用）。

## 8. 前端现状

- Next.js App Router，静态导出（`NEXT_EXPORT=1`）后由 FastAPI 同源提供；开发模式通过 rewrite 代理 `/api`。
- 页面：`/`（Profiles）、`/proxies`、`/groups`、`/schedules`、`/templates`、`/extensions`、`/access`、`/settings`、`/trash`。
- 组件风格：Tailwind v4、lucide-react 图标、自有 `modal`/`toast`/`empty-state`；API 客户端在 `src/lib/*-api.ts`。
- i18n：`src/lib/locales/zh-CN.json`、`ru.json`；`web/tests/i18n.test.ts` 强制**所有字面翻译调用在每个语言目录都有条目**、
  键集合一致——新增 UI 文案必须同时补中文与俄文。
- 未发现 CSP 头限制 Web UI（只有 `desktop_recovery.py` 的独立恢复页有 CSP）。MapLibre 需要 Web Worker 与外部瓦片源，
  静态导出与 pywebview 下的可用性、离线降级需在第 4 阶段实测。
- 地图库与瓦片源：尚未选型；`maplibre-gl`/`react-map-gl` 需选与 React 19 / Next 16 兼容的版本，并核实瓦片服务条款。

## 9. 兼容风险

1. **数据库版本提升**：迁移 6 后旧版本管理器无法打开数据库（与已有策略一致，需在发布说明中写明）。
2. **`pyyaml` 不是核心依赖**：目前只随 `chrome-migration` extra 安装；订阅解析需要把 `pyyaml` 加入核心依赖并更新 `uv.lock`，
   否则打包版本缺少它。只能使用 `yaml.safe_load`（或 `CSafeLoader`）。
3. **未配置密钥时加密为恒等变换**：节点配置与订阅 URL 必须强制要求密钥，否则明文落库。
4. **SSRF 与 DNS 重绑定**：httpx 默认自行解析域名；“先解析校验再请求”存在重绑定窗口。需要在连接层校验实际对端地址
  （自定义 httpcore 网络后端或固定 IP + SNI），并手动逐跳处理重定向（`follow_redirects=False`）。
5. **订阅拉取走本机直连**：拉取订阅会向机场暴露本机公网 IP，这是固有行为，需要在 UI/文档说明。
6. **Mihomo DNS 与 UDP**：浏览器经 HTTP 入口访问时，目标域名由 CONNECT 交给 Mihomo，`MATCH` 规则不需要本地解析；
   但 Mihomo 自身 DNS 配置（DoH、`proxy-server-nameserver`、`redir-host`）的真实行为必须用目标版本 `mihomo -t` 与集成测试确认，
   不能只照抄示例配置。HTTP 入口不承载 UDP；WebRTC 仍由 Camoufox 的 `webrtc:ipv4` 伪装与 Profile 设置控制，需单独测试。
7. **本机入口可被其他本地进程使用**：与现有 `SocksRelay` 一样，loopback 监听不是安全边界（文档已有说明，需沿用）。
8. **城市级定位**：已确认 AIO 库没有城市字段（第 5.5 节），开发文档的 `precision=city` 需降级为 `coordinates`。
9. **多实例**：`/network/runtime` 与 Mihomo 进程只属于本实例；另一实例启动同一 Profile 会被 lease 拒绝（现有行为）。
10. **备份 / 归档 / 同步**：Profile 归档与同步快照只包含 `proxy_config` 副本；新绑定与节点（实例级密钥，类似代理库）不进入归档与同步，
   导入到另一台机器后需要重新绑定。需在文档中写明，避免用户以为绑定会随 Profile 迁移。
11. **孤儿进程**：管理器崩溃后 Mihomo 子进程可能残留。需要不含密钥的短期运行登记 + 启动时回收（开发文档第 6 节），
   且只回收由本程序启动、命令行/路径匹配的进程，不能误杀用户自己的 Mihomo。
12. **远程仓库是上游**：`origin` 指向 polyackiy 上游，推送前需配置个人 Fork。
13. **本机 mypy 环境差异**：安装 `desktop` extra 时 mypy 多报 1 个既有错误（见第 2 节），CI 配置下不出现。
14. **系统代理**：未设代理的 Profile 和默认 httpx 客户端都会被系统/环境代理接管（第 5.6 节），影响“直连”的含义、SSRF 校验与端口分配。
15. **macOS 上取不到浏览器 pid**：会话清理只依赖 Playwright 事件（第 2 节），Mihomo 生命周期不能挂在浏览器 pid 上。

## 10. 下一阶段（第 1 阶段：订阅与节点数据）准确文件清单

| 文件 | 改动 |
| --- | --- |
| `pyproject.toml`、`uv.lock` | 核心依赖加入 `pyyaml`（锁定已在 lock 中的 6.0.3 范围） |
| `src/camoufox_pm/core/schema_migrations.py` | 迁移 6：四张新表 + 索引 + `manager_features('network-nodes',1)`，`CURRENT_SCHEMA = 6` |
| `src/camoufox_pm/core/network_subscription.py`（新） | HTTPS-only 拉取、`User-Agent: clash.meta/v1.18.0`、SSRF/重定向/DNS 校验、限时限大小、`subscription-userinfo` 解析、`yaml.safe_load`、只取根 `proxies`、`proxy-providers`-only 与 Base64/URI 格式明确报错 |
| `src/camoufox_pm/core/network_node_catalog.py`（新） | 协议白名单与必填字段校验、规范化、`node_key` 哈希（不含密码）、加密存储（复用代理库的“无密钥即拒绝”）、原子增量同步（保留 ID、退役缺失节点、同步失败不清空已有节点） |
| `tests/unit/test_schema_migrations.py` | 新增迁移 6 的升级、重复执行、旧版本拒绝用例 |
| `tests/unit/test_network_import.py`（新） | 空配置、合法配置、UA 请求头、同名多来源、重复同步 ID 不变、退役、SSRF（私网/回环/元数据/重定向到 127.0.0.1）、超长响应、恶意 YAML（自定义 tag、锚点炸弹）、只有 provider、Base64、特殊字符、明文泄露检查（日志/返回值/数据库无明文） |
| `tests/unit/test_network_catalog.py`（新） | 节点加密、无密钥拒绝、`node_key` 稳定性、绑定节点退役后状态 |

第 1 阶段不改动 `/api/v1/proxies`、`/api/v1/profiles/*` 与启动路径；API 路由放在第 3 阶段（若需提前提供订阅接口，
新增 `src/camoufox_pm/api/routes/network.py` 与 `api/models/network.py`，并在 `main.py` 的注册循环中加入）。

后续阶段预计涉及（供评估，不在第 1 阶段修改）：`core/mihomo_manager.py`、`core/network_adapter.py`（新）；
`core/browser_session.py`（`BrowserSession` 持有网络运行时并在 `_terminate` 中关闭）；`core/profile_manager.py`
（`_launch_browser` 中 lease 之后调用适配器）；`core/network_binding.py`、`core/network_exit_probe.py`（新）；
`main.py` 路由注册；`web/src/app/map/`、`web/src/app/subscriptions/`、`web/src/components/network/`、`src/lib/network-api.ts`、
两份 locale 文件；`packaging/camoufox-pm.spec`（若捆绑 Mihomo）。

## 11. 测试计划

- **每阶段**：`ruff check`、`ruff format --check`、`mypy src/camoufox_pm`、`pytest -m "not browser"`（带 CI 的 ResourceWarning 参数），
  前端阶段加 `npm test`、`npm run lint`、`npm run build` 与 `scripts/build_webui.py`。
- **单元测试**：Mock Mihomo 二进制（假可执行脚本，验证参数数组、生成配置、端口就绪与回收）与 mmdb（Mock `locate`），
  使用 `--no-network`，不依赖外网。
- **集成测试**：需要真实 `mihomo` 二进制。将提供准备脚本（下载指定版本、校验 SHA-256），缺二进制时测试明确失败并提示脚本，
  不静默跳过、不以假数据冒充通过。本地受控上游用回环 SS/HTTP 测试服务器，验证 A/B 两个 Profile 走各自端口、A 失效不影响 B、
  Mihomo 被杀后无直连、批量检测只启动一个进程、结束后无残留进程与监听端口。
- **浏览器测试**（`-m browser`）：需要 `camoufox fetch` 与 GeoIP 库；覆盖 Cookie/LocalStorage/IndexedDB 隔离、绑定后启动、关闭重开。
- 浏览器 TLS 代理验收（`tests/browser/test_proxy_protocols.py`）需要 NSS certutil（macOS：`brew install nss`）；本机未安装，11 个用例跳过。

## 12. 第 0 阶段未完成项

- `test_cookie_transfer.py` 中 1 个浏览器测试因本机系统代理失败（第 2 节），未在关闭系统代理的条件下复测。
- 11 个浏览器 TLS 代理验收测试因缺 NSS certutil 跳过。
- Mihomo 未安装（第 2 阶段才需要）。
- 双 Profile 基线使用 API + 有界面浏览器完成，未通过桌面壳（pywebview）窗口手动点击验证。

## 13. 审计之后

第 1–5 阶段已在同一分支实现，结果与仍未验证的事项见 [network-test-report.md](network-test-report.md)，
设计见 [network-architecture.md](network-architecture.md)，接口见 [network-api.md](network-api.md)。
审计中的两个预判已被实测修正：GeoIP AIO 没有城市字段（`precision` 采用 `coordinates`）；
本机桌面客户端自带的 Mihomo 是 setuid root，管理器会拒绝它，需用 `scripts/prepare_mihomo.py` 复制普通副本。
