# 网络节点 / 地图改造：统一测试报告

测试日期：2026-10-29。主机：macOS 27.0.1，Apple Silicon（arm64），Python 3.12.15，Node 26.12.1，
Camoufox 0.5.8 + 浏览器 156.0.1-beta.36，GeoIP AIO，Mihomo Meta v1.19.32（由 `scripts/prepare_mihomo.py --from`
从本机代理客户端附带的内核复制的非 setuid 副本）。基线为上游 HEAD `ea62ee1`。

**本机环境特点**：开启了系统代理 `127.0.0.1:7890`（本机 Clash 系客户端），并设置了 `HTTP(S)_PROXY`。下文两个已知失败都由此引起。

## 1. 结果汇总

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| Ruff lint | `uv run ruff check src tests scripts/configure_ci_apt.py scripts/prepare_mihomo.py` | 通过 |
| Ruff format | `uv run ruff format --check …` | 通过 |
| mypy | `uv run mypy src/camoufox_pm` | 1 个错误，**既有、环境相关**（`desktop_recovery.py:156`，仅在安装 desktop extra 时出现；CI 不安装） |
| mypy（新模块严格） | `uv run mypy --check-untyped-defs <8 个新模块>` | 通过 |
| 后端全部非浏览器测试（含真实 Mihomo 集成） | `uv run pytest -m "not browser" --no-network -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning -o faulthandler_timeout=300` | **2538 passed，1 failed，6 skipped**（随后新增迁移测试后再 +1 passed） |
| 浏览器测试（真实 Camoufox） | `uv run pytest -m browser --no-network -W error::ResourceWarning` | **37 passed，1 failed，13 skipped** |
| 前端单测 | `cd web && npm test` | 31/31 通过（含 i18n 完整性检查与新增地图逻辑测试） |
| 前端 lint / 类型 | `npm run lint`、`npx tsc --noEmit` | 通过 |
| 前端构建 | `npm run build`、`uv run python scripts/build_webui.py` | 通过 |

基线对比：改动前 `pytest -m "not browser"` 为 2401 passed；改动后原有用例全部仍通过（中途发现并修复了两处回归，见第 4 节）。
浏览器测试基线 34 passed，改动后 37 passed（新增 3 个）。

### 两个已知失败（均为本机系统代理所致，非本次改动引入）

1. `tests/browser/test_cookie_transfer.py::test_cookie_import_is_sent_with_scope_flags_and_sessions_end_on_close`
   —— 第 0 阶段已记录：Firefox 跟随系统代理把合成域名 `cookie.test` 发往 7890，后者返回 502。
2. `tests/test_no_network_guard.py::test_the_guard_outranks_a_module_that_swallows_failures`（只在 `--no-network` 下运行）
   —— `proxy=None` 时 httpx 读取 `HTTP_PROXY=127.0.0.1:7890`，请求发到被允许的回环地址，所以守卫没有触发。
   验证：`git stash` 回到原始 `proxy_check.py` 时同样失败；去掉代理环境变量后当前代码 2/2 通过。CI 不带 `--no-network` 跑这组，会跳过。

## 2. 新增测试（138 个）

| 文件 | 数量 | 覆盖 |
| --- | --- | --- |
| `tests/unit/test_network_import.py` | 57 | 合法 YAML（只取 proxies、丢弃 rules/dns/tun/controller、去掉 dialer-proxy/interface-name/本地文件路径、保留内联 PEM 与 WireGuard 私钥）；空/编码错误/坏 YAML/非映射/无 proxies/仅 provider/分享链接/Base64/全部无效 → 各自错误码；自定义 YAML 标签；别名炸弹；条目与节点数量上限；同名多条目稳定且唯一的 key；key 不含密码；错误信息不回显密码；URL 校验；地址策略（回环/私网/CGNAT/元数据/组播/IPv6 本地/IPv4 映射）；DNS 结果混入私网即拒绝；**真实本地 TLS 服务器**上验证 `User-Agent: clash.meta/v1.18.0`、`subscription-userinfo`、文件名、gzip 解压与解压炸弹、超大响应截断、每跳重定向重新校验（http、127.0.0.1、169.254.169.254）、重定向环、证书不受信且不回显 token、HTTP 错误与超时 |
| `tests/unit/test_network_catalog.py` | 12 | 订阅 URL 与节点密码只以密文入库（整库 dump 检查）、列表不含秘密；无密钥拒绝保存；首次导入失败不留数据；同步保留 ID、退役缺失节点、节点复活同 ID；配置变化原地更新并清除旧检测；拉取或解析失败不动已有节点；文件订阅需新文件；有绑定时删除需显式退役且被绑定节点保留；无绑定时级联删除；Profile 进回收站保留绑定、永久删除级联；地图只放已测出口并统计共用 IP；按测得数据过滤；错误密钥报错而非返回密文 |
| `tests/unit/test_mihomo_config.py` | 26 | 生成配置只在回环、带认证、`MATCH,REJECT`、无控制端口/全局入口/fake-ip/TUN；12 项安全不变量逐一被篡改时均被拒绝；批量 listener 一一对应；二进制校验（版本过低、非 Mihomo、他人可写、缺失、setuid、显式配置损坏）；日志分类且不回显地址；孤儿回收：管理器已死的进程被终止、存活管理器的保留、PID 被复用的用户进程不被触碰、刚创建未写 owner 的目录保留 |
| `tests/unit/test_ip_intel.py` | 13 | ipinfo / IPPure / Scamalytics（v3 与旧格式）解析；恶意值被丢弃；综合分（干净住宅原生 = 88；机房+广播+VPN+高欺诈 = 0）；无信号时不给满分；数据源看到不同 IP 时告警；检测网站白名单；**经本地代理确认每个数据源请求都走节点**；限流/非 JSON/数据源错误被隔离且 Scamalytics key 不出现在结果中；禁用的数据源不被访问；超大响应被拒；数据源城市只在与 GeoIP 坐标一致（≤150 km）时采用 |
| `tests/integration/test_network_runtime.py`（真实 Mihomo） | 11 | 两个 Profile 经各自节点、端口、凭据、进程，geoip 取各自出口；凭据交叉使用被拒；重复启动为 already_running；杀掉 A 的引擎 → A 降级且无法联网、B 不受影响、重连恢复；节点不可达时不启动浏览器、释放 lease、无残留进程；关闭浏览器后进程退出、私有目录删除、绑定保留；运行中改绑被拒；退役节点启动返回 422；未绑定 Profile 仍使用手动代理且不启动 Mihomo；**批量 3 个节点只用 1 个进程**；Mihomo 拒绝某节点配置时顺序重启并跳过该节点；单节点检测复用运行中的 listener；IP 质量查询经节点 |
| `tests/integration/test_api_network.py` | 17 | 包装/no-store/无秘密泄露/别名路径；各类来源错误码且不存数据；请求体互斥校验；无密钥 409；绑定 CAS、地理冲突与手动代理提示（不自动修改）、运行中 409、解绑版本检查；缺失 Profile/节点 404、退役节点 422；有绑定订阅删除流程；改名版本检查；设置密钥只写且密文存储、范围与 https 校验、清除；运行时动作需运行中；批量参数校验；运营者/匿名访问被拒，可见自己 Profile 的运行时；**无 Mihomo 时启动绑定 Profile → 503 且释放 lease** |
| `tests/unit/test_schema_migrations.py` | +1 | schema 5 数据库升级到 6：建表、写入 feature、保留已有 Profile、外键检查通过、可重复执行 |
| `tests/browser/test_profile_proxy_isolation.py`（真实 Camoufox + Mihomo） | 3 | 页面读到的出口分别为各自节点；A 的节点到不了 B 的源站；浏览器时区来自测得出口；**杀掉引擎后浏览器报代理错误，源站零请求（无直连、无系统代理回退）**，另一个 Profile 正常，重连恢复；Cookie / localStorage / IndexedDB 不跨 Profile 且重启后保留 |
| `web/tests/network-map.test.ts` | 6 | 同坐标节点合并为一个可选地点并按可用性/延迟排序；无效坐标不绘制；IP 打码；纯净度色阶与旗帜；流量用量；外部查询只接受 IP |

## 3. 端到端实测（真实应用 + 真实网络）

使用独立的演示实例（临时数据库与密钥，不触碰用户数据），在应用内浏览器中操作 Web UI：

1. **导入**：粘贴 YAML（两个指向本机代理客户端 `127.0.0.1:7890` 的节点、一个死节点、一个 `type: direct` 条目，
   并带有 `external-controller`、`rules: MATCH,DIRECT` 等应被忽略的字段）→ 报告“新增 3、跳过第 4 项：不支持的协议”。
2. **批量检测**（单进程）：两节点出口为同一个美国 IP（下文记作 `203.0.113.21`，实测值已隐去）、延迟约 1.5 s，并标注“2 个节点共用”；死节点“不可用”。
3. **地图**：国家边界离线绘制，美国位置出现绿色“2”标记；点击后右侧列出该坐标下**两个**节点；死节点在“未定位”。
4. **IP 质量**（经节点真实查询 ipinfo.io 与 IPPure）：纯净度 98“非常纯净”，IPPure 欺诈分 2，住宅、原生，美国家庭宽带运营商 ASN，
   反向解析主机名可见。ipinfo 称该 IP 在加州某城市，而 GeoIP 坐标在美国中西部（反向解析主机名也指向中西部）——
   界面显示“数据源归属地”并提示相差约 2760 km，城市不被错误地标在 GeoIP 点上（此问题在实测中发现并修复）。
5. **绑定与启动**：选择 Profile A → 绑定 → 启动真实 Camoufox（约 9 s）。运行时 `running / mihomo / exit 203.0.113.21`；
   `lsof` 确认独立 `mihomo` 进程只监听 `127.0.0.1:<端口>`，Camoufox 的连接全部指向该端口。
6. **检测网站**：点击 IPPure → 在 Profile A 自己的浏览器里打开。
7. **停止**：Mihomo 进程退出、端口释放、私有运行目录清空、运行时 `stopped`、绑定保留；
   用户自己的代理客户端进程未受影响。

## 4. 测试中发现并已修复的问题

| 问题 | 发现方式 | 修复 |
| --- | --- | --- |
| `resolve_exit_ip` 新增默认参数在导入时固定了端点，原有测试运行时修补 `IP_ENDPOINTS` 失效 | 原有测试回归 | 默认值改为 `None`，调用时读取模块变量 |
| 启动时总向 `browser_sessions.launch` 传 `network=` 参数，47 个原有测试替身签名不匹配 | 原有测试回归 | 仅在有绑定时才传，未绑定 Profile 的调用形态完全不变 |
| 地图页一次请求 200 个 Profile，超过接口上限 100 | 真实应用 | 按 100 分页拉取 |
| MapLibre 样式 `.maplibregl-map{position:relative}` 覆盖容器绝对定位，高度为 0，地图永不加载 | 真实应用 | 容器使用内联定位 |
| 数据源城市被贴到另一数据源的坐标上 | 真实网络数据 | 仅在两者相距 ≤150 km 时采用，否则单独显示并提示差异 |
| 初始视野不含已测出口 | 真实应用 | 首次加载自动缩放到全部出口；窄屏改为上下布局 |

（应用内浏览器面板隐藏时 `requestAnimationFrame` 不执行，地图会停在加载状态——这是测试环境现象，面板显示后正常。）

## 5. 未验证 / 未完成项（如实列出）

- **Windows / Linux / Intel Mac 未实测**；未构建或验证任何安装包，也未把 Mihomo 打包进安装包（GPL-3.0 分发义务需单独决定）。
- **CI 未运行真实 Mihomo 测试**：CI 机器没有 Mihomo，已在 `ci.yml` 与 `browser-compatibility.yml` 中排除 `mihomo` 标记
  （这些测试缺二进制时会**失败**而不是跳过）。若要在 CI 中运行，需要提供带固定 SHA-256 的下载步骤。
- 真实机场协议（vmess/vless/trojan/hysteria2 等）的出站只验证了配置被 Mihomo 接受/拒绝的路径；端到端流量测试使用的是
  SOCKS5/HTTP 上游（本地夹具与本机 Clash），没有真实机场账号。
- WebRTC STUN、IPv6、QUIC、WebSocket 泄露未做专项测试（HTTP 入口不承载 UDP；WebRTC 由 Camoufox 按测得出口伪装）。
- Scamalytics 只按其文档字段实现解析，**没有真实 API key 实测**；IPIP.la 无公开 API，仅支持在 Profile 浏览器中打开。
- 10 个 Profile 并发、长时间运行、管理器被强杀后的孤儿回收只做了单元级验证（真实进程 + 伪造 owner），未做强杀整机实测。
- 订阅拉取不走代理：如果机场订阅域名在本地网络无法直连，需要改用“导入文件”。
- 桌面壳（pywebview 窗口）中未手动点测，Web UI 在浏览器中完成了实测。
- 俄语翻译由开发者完成，未经母语者校对。
