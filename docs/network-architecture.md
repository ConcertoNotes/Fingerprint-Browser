# 订阅节点、出口地图与 Profile 绑定：架构说明

本文描述**已经实现**的设计（不是计划）。改造前的审计见 [network-implementation-audit.md](network-implementation-audit.md)，
接口见 [network-api.md](network-api.md)，测试结果见 [network-test-report.md](network-test-report.md)。

## 1. 总览

```text
Web UI（Next.js 静态导出，由 FastAPI 同源提供）
  /map            世界地图 + Profile 侧栏 + 节点详情（出口 IP / 位置 / IP 质量 / 绑定 / 检测网站）
  /subscriptions  订阅 URL / 文件导入、同步、节点表、批量检测
  /               原 Profile 列表新增“代理 / 节点”列（当前节点、出口、网络状态、更换节点）
  /settings       网络设置：Mihomo 路径与自检、GeoIP 状态、并发/超时、IP 质量数据源密钥
        │  /api/v1/network/*（新增，沿用 ApiResponse 包装、鉴权与错误体）
        ▼
core/network_subscription.py  HTTPS 拉取（SSRF 套接字级防护）+ 安全 YAML 解析 + 协议白名单
core/network_catalog.py       订阅/节点的密封存储、原子增量同步、检测结果、IP 情报缓存、地图数据
core/network_settings.py      实例级网络设置（密钥只写）
core/network_binding.py       Profile ↔ 节点 CAS 绑定（复用 profile_operation：运行中拒绝）
core/mihomo_manager.py        Mihomo 二进制校验、安全配置生成与断言、进程/端口/目录生命周期、孤儿回收
core/network_adapter.py       启动时的网络运行时（每个运行中的绑定 Profile 一个 Mihomo）
core/network_probe.py         出口检测：单节点 / 单进程多 listener 批量池
core/ip_intel.py              经节点查询 ipinfo / IPPure / Scamalytics，综合纯净度
        │
        ▼
SQLite（迁移 6 新增：network_subscriptions / network_nodes / network_node_checks /
        network_ip_intel / profile_network_bindings / network_settings）
```

`profile_manager._launch_browser` 与 `browser_session` 只增加了一个可选的网络运行时；未绑定节点的 Profile 走原来的代码路径，
调用参数也完全不变（现有测试替身无需修改即可通过）。

## 2. 启动状态机（真实实现）

```text
STOPPED
  │ POST /profiles/{id}/launch
  ▼
profile_admission（跨进程文件锁） → acquire_lease（lease 是“是否运行”的权威）
  │ 已在本进程运行 → already_running（不再准备网络）
  ▼
PREPARING_NETWORK  NetworkAdapter.prepare()
  │  无绑定 → 原手动代理 / 直连路径（行为不变）
  │  节点 retired/invalid → 422 node_retired / node_invalid
  │  Mihomo 不可用 → 503 binary_* / engine_*
  │  启动独立 Mihomo（1 个 outbound + 1 个带随机凭据的 127.0.0.1 listener），确认监听套接字属于该进程
  ▼
VERIFY_EXIT        经该 listener 查询出口 IP；失败 → 502（分类：node_unreachable / node_tls_failed …）
  ▼
READY              options.proxy = 本 Profile 的 listener（覆盖任何 additional_options 代理）
  │                geoip = 刚测得的出口 IP（Camoufox 据此推导时区、位置、WebRTC）
  ▼
RUNNING            BrowserSession 持有运行时；浏览器关闭时先关浏览器再关 Mihomo
  │ Mihomo 意外退出
  ▼
DEGRADED           listener 消失 → 浏览器连接失败（不回退直连、不回退系统代理、不换节点）
  │ POST /network/runtime/{id}/reconnect：同节点、同端口、同凭据重启
  ▼
STOPPING → STOPPED 进程终止（terminate → 3 s → kill）、私有目录删除、lease 释放；绑定保留
```

任何一步失败：已启动的 Mihomo 被关闭，lease 被释放，异常原样上抛（启动路由映射为对应状态码）。

## 3. 每个 Profile 独立入口（不使用全局选择器）

- 每个运行中的绑定 Profile 有自己的 Mihomo 进程，配置中只有一个 outbound（`node-0`）和一个 listener，
  listener 直接用 `proxy: node-0` 指定出站，**不经过** `GLOBAL`/`proxy-groups` 等可变选择器。
- listener 只监听 `127.0.0.1`，并要求随机用户名/密码（`users`）。实测（Mihomo v1.19.32）：
  不带凭据 → `407`，用另一个 Profile 的凭据 → `403`。因此即使端口被复用或被本机其他程序抢占，
  也不会“静默”把一个 Profile 的流量送进另一个出口。
- 浏览器拿到 `http://127.0.0.1:<port>` + 凭据，由 Playwright 完成代理认证。
- 这仍然**不是**操作系统级隔离：本机恶意进程、上游机场的关联分析、浏览器漏洞都不在防护范围内。

## 4. 生成的 Mihomo 配置与安全不变量

`mihomo_manager.runtime_config()` 生成配置后立即调用 `validate_runtime_config()` 断言：

| 不变量 | 目的 |
| --- | --- |
| 无 `external-controller*`、`port`/`socks-port`/`mixed-port`/`redir-port`/`tproxy-port` | 没有控制端口、没有全局入口 |
| `allow-lan: false`、`bind-address: 127.0.0.1`、listener `listen: 127.0.0.1` | 只在回环 |
| 每个 listener 都有 `users`，且 `proxy` 指向本配置内的 outbound | 认证、一一对应 |
| 无 `proxy-groups` / `proxy-providers` / `rule-providers` / `script` | 无可变选择器、无远程规则 |
| `rules: [MATCH,REJECT]`，outbound 中无 `direct/reject/dns/pass` 类型、无 `dialer-proxy` | 节点不可用时失败，而不是直连或链到别处 |
| `tun.enable: false`、`sniffer.enable: false`、`find-process-mode: off` | 不碰系统网络、不触发 macOS 权限提示 |
| `dns.enhanced-mode: redir-host`（禁止 fake-ip），`profile.store-fake-ip: false` | 不产生 198.18.x.x 污染 Camoufox geoip |

配置以 JSON 写入（JSON 是 YAML 子集，避免机场字符串触发 YAML 标签/锚点/隐式类型），
文件权限 0600，目录 0700（复用 `update_permissions.create_private_directory/file`）。
进程以参数数组启动，环境变量被清空为最小集合（**不继承 `HTTP(S)_PROXY`**），`HOME/TMPDIR` 指向私有目录。

### DNS 防泄露的真实机制

- 浏览器使用 HTTP 代理：目标主机名随 `CONNECT host:port` 交给 Mihomo，listener 直接转发给节点，
  **目标域名在节点远端解析**。实测：上游 SOCKS5 节点收到的是 `dest-a.test` 域名而不是 IP。
- Mihomo 自身只需要解析**节点服务器**的域名：使用 `proxy-server-nameserver`（加密 DoH：`223.5.5.5`、`1.12.12.12`）
  与 `nameserver`（`1.1.1.1`、`8.8.8.8` 的 DoH），均为 IP 形式，避免明文引导查询。
- 不使用 fake-ip；`MATCH` 规则不需要本地解析目标。
- 未覆盖：UDP（HTTP 入口不承载 UDP，listener `udp: false`）、QUIC（浏览器通过 HTTP 代理不会走 QUIC）。
  WebRTC 由 Camoufox 根据测得的出口 IP 伪装（`webrtc:ipv4`），未单独做 STUN 泄露测试，见测试报告。

## 5. 订阅导入安全

- 只接受 `https://`；URL 中不允许账号密码、空白和控制字符。
- 拉取直接使用固定版本的 `httpcore`，自定义网络后端在**每次连接**（包括每一跳重定向）时自己解析域名，
  只有当**所有**解析结果都是公网地址时才连接，并且连接的正是校验过的地址 → 关闭 DNS rebinding 窗口。
  拒绝回环、私网、链路本地（含 169.254.169.254 元数据）、CGNAT、组播、保留地址、IPv4 映射的 IPv6。
- 不读取环境代理（否则地址校验对实际连接无效）。**订阅从本机直连拉取**，会向机场暴露本机公网 IP。
- 请求头 `User-Agent: clash.meta/v1.18.0`，读取 `subscription-userinfo`（流量/到期）与 `content-disposition`（默认名称）。
- 限制：最多 3 次重定向、30 s 总超时、10 MB 下载、gzip/deflate 解压后 20 MB、拒绝其他压缩编码。
- YAML：`SafeLoader` 子类，限制节点数（50 万）与别名数（200），自定义标签直接失败；错误信息不引用原文（避免泄露密码）。
- 只读取根 `proxies`；`proxy-providers` 单独存在 → `providers_only`；分享链接列表 → `uri_list_unsupported`；
  Base64 订阅 → `base64_unsupported`；绝不会“成功导入 0 个节点”。
- 协议白名单：`ss ssr vmess vless trojan hysteria hysteria2 tuic anytls snell wireguard socks5 http`，
  按协议检查必填字段；丢弃 `dialer-proxy`、`interface-name`、`routing-mark`、`provider`；
  `ca/certificate/private-key` 等字段若是本地路径则丢弃（保留内联 PEM，保留 WireGuard 的私钥字符串）；
  值递归限制为纯数据（深度 5、字符串 8 KB、单节点 32 KB）。每条被跳过的条目只报告序号和原因，不回显值。
- `node_key` = sha256(协议|服务器|端口|名称) 前 32 位，**不含密码**；同一订阅内完全相同的条目追加 `#2`、`#3`。

## 6. 存储与同步

- 节点配置、订阅 URL、数据源密钥使用原有 Fernet 层密封；**没有加密密钥时拒绝保存**（`secrets_unavailable`），
  不会像 `crypto.encrypt` 那样退化为明文。解密失败报错，而不是把密文当作配置。
- 同步在一个 `BEGIN IMMEDIATE` 事务内完成：存在的节点保留 ID（以及绑定、检测历史）；配置变化时更新并清除旧检测
  （同名节点改了密码/参数，出口可能已经不同）；消失的节点标记 `retired`；拉取/解析失败只把订阅标记为 `error`，
  已有节点不变。
- 删除订阅：没有绑定 → 连同节点硬删除；有绑定 → 默认 409 `subscription_bound`，显式 `retire_bound=true`
  时把被绑定的节点保留为 `retired`、删除其余节点、清除订阅 URL 密文并软删除订阅。
- `profile_network_bindings.node_id … ON DELETE RESTRICT`；Profile 永久删除时绑定级联删除；进回收站时绑定保留。
- 迁移 6 写入 `manager_features('network-nodes', 1)`：旧版管理器会拒绝打开新数据库（与迁移 3/4/5 相同的策略）。
- 绑定与节点是实例级数据（像代理库一样），**不随 Profile 归档/同步迁移**；导入到另一台机器后需重新绑定。

## 7. 出口检测与地图定位

- 出口 IP 一律**经由节点本身**查询（复用 `proxy_check.resolve_exit_ip`：TLS 校验、≤256 字节响应、整端点超时）。
  入口服务器地址、节点名、旗帜 emoji 从不参与定位。
- 定位使用 Camoufox 自带的 GeoIP 库（默认 GeoIP AIO）——与浏览器启动时推导时区用的是同一份数据。
  该库只有国家与坐标/时区，**没有城市名**，因此 `precision` 为 `coordinates`（近似）。
- 第三方数据源（ipinfo/IPPure）给出城市：只有当其坐标与 GeoIP 点相距 ≤150 km 时才作为该点的城市名；
  否则城市留空，界面单独显示“数据源归属地”并提示两者相差多少公里（实测某住宅出口：GeoIP 在美国中西部，
  ipinfo 称在加州某城市，相差约 2760 km）。GeoIP 查不到时才用数据源坐标落点，并标注来源 `intel`。
- 查不到位置的节点进入“未定位”列表，绝不按名称点亮。检测超过 24 小时显示为过期（黄色）。
- 同一出口 IP 的节点数（`shared_exit_count`）随节点返回，界面明确提示“不同节点不等于不同公网 IP”。

### 批量探测池（防进程风暴）

`check-batch`（≤200 个节点，后台任务 + 轮询）：

1. 读取所有节点配置，生成**一个**临时 Mihomo 配置：N 个 outbound、N 个带独立凭据的 listener；
2. 只启动 1 个进程；若 Mihomo 因某个节点配置拒绝启动（日志 `proxy N:`），记录该节点为 `config_invalid`，
   去掉它后重启（顺序进行，任一时刻只有 1 个进程；最多 6 次）；
3. 以可配置并发（默认 8）经各 listener 查询出口；
4. 结束（完成/失败/取消）后销毁进程与目录。批量检测**从不**调用第三方 IP 质量数据源。

单节点检测：若该节点正被某个运行中的 Profile 使用，直接复用那个 listener；否则临时启动一个单 listener 进程。

## 8. IP 质量（纯净度、欺诈分数）

| 数据源 | 接入方式 | 何时调用 |
| --- | --- | --- |
| ipinfo.io | 官方 `https://ipinfo.io/json`（可选 token；付费 token 才有 privacy 标记） | 单节点检测，经节点 |
| IPPure | 公开接口 `https://my.ippure.com/v1/info`（返回调用者 IP 的 fraudScore / isResidential / isBroadcast） | 单节点检测，经节点 |
| Scamalytics | 官方 API（用户自己的 API URL + key） | 配置后，单节点检测，经节点 |
| IPIP.la | 无公开 API（页面使用短期签名 token 和人机验证），**不做自动查询** | 在运行中的 Profile 浏览器里打开 |
| Scamalytics 网页 | 不抓取 | 在 Profile 浏览器里打开 `scamalytics.com/ip/<ip>`，或外部链接 |

- 所有查询都经节点发出：数据源看到的是出口，而不是本机；同时避免把本机 IP 与该代理 IP 关联。
- 遵守条款：IPPure 条款禁止未经授权的高频/批量自动访问，因此仅在用户点击单个节点时调用，按 IP 缓存
  （默认 24 小时），每个数据源串行且至少间隔 1.5 s，批量检测不调用。
- 响应体 ≤64 KB、只接受 JSON、错误只记录分类（`rate_limited` / `unauthorized` / `timeout` …），
  从不回显请求 URL（Scamalytics key 在查询串中）。
- 综合分（启发式，界面同时展示每个原始值）：
  `风险分 = max(IPPure fraudScore, Scamalytics score)`；
  `纯净度 = clamp(100 − 风险分 − 机房 10 − 广播 10 − VPN/代理/Tor 标记 15, 0, 100)`；
  分级：≥80 非常纯净、≥60 纯净、≥40 一般、≥20 有风险、其余高风险；没有任何信号时为“未评估”，而不是 100。
- “检测网站”按钮在**所选且正在运行的 Profile 浏览器**中打开 IPPure / IPIP.la / ipinfo / Scamalytics，
  即经该 Profile 的出口访问（URL 来自固定白名单）。另有“从本机查询”的外部链接，界面注明不经过节点。

## 9. 地图

- MapLibre GL（BSD-3）绘制随包附带的 Natural Earth 国家边界（公有领域，world-atlas 110m），
  **不依赖任何在线瓦片服务**，离线可用，无需 API key。
- MapLibre 的 worker 由 `web/scripts/copy-vendor.mjs` 在每次 dev/build 前从 node_modules 复制到
  `public/vendor/maplibre/`（被 git 忽略），并通过 `setWorkerUrl` 指定，保证版本一致；`.mjs` 显式注册为 JavaScript MIME。
- 先按坐标（4 位小数）把节点分组为“地点”，再用 supercluster 聚合邻近地点。点击地点 → 右侧列出**该坐标下全部节点**，
  解决同城多节点完全重叠、只能点到最上层的问题；点击区域聚合 → 放大。
- 标记是可聚焦、带文字标签的 `<button>`；右侧有完整的可键盘操作地点列表和“未定位”列表；图例带文字；
  WebGL 不可用时显示说明，列表仍可用。
- 首次加载自动缩放到所有已测出口；宽度不足 1280 px 时地图在上、面板在下。

## 10. 孤儿进程回收

每个运行时目录写入 `owner.json`（管理器 pid + 启动时间、Mihomo pid + 启动时间、配置路径，不含密钥）。
`ProfileManager.initialize()` 时回收：只处理**发起它的管理器已不存在**的目录；只终止 pid、启动时间与命令行
（包含该配置路径）都匹配的进程——用户自己运行的 Mihomo/Clash 客户端永远不会被触碰；
刚创建尚未写入 owner 的目录（<60 s）保留给仍在启动的其他实例。

## 11. 二进制要求

- Mihomo ≥ 1.18，自动查找顺序：设置中的路径 → `CPM_MIHOMO_PATH` → 打包目录 → `<数据目录>/bin/` →
  源码目录 `.cache/mihomo/` → `PATH`。
- 拒绝 setuid/setgid、组/其他用户可写、属于其他用户（非 root）的文件。很多桌面客户端自带的 sidecar
  （某些客户端附带的 `sidecar/mihomo`）是 **setuid root**——直接使用会让每个 Profile 的代理以 root 运行，
  所以必须用 `scripts/prepare_mihomo.py --from <路径>` 复制一份普通用户副本。
- 安装包**没有**内置 Mihomo（GPL-3.0，分发需履行源码义务，需单独决定）。

## 12. 与原开发文档的差异

| 原文档 | 实际实现 | 原因 |
| --- | --- | --- |
| `precision: city` 来自 GeoLite2-City | `coordinates`（GeoIP AIO 无城市字段）；城市来自数据源且需坐标一致 | 实测数据库结构 |
| 示例配置 `mixed-port` + `proxy-groups` + `external-controller` | 无全局入口、无选择器、无控制端口；listener 直连 outbound + 认证 | 更强隔离；实测验证 |
| `proxy-server-nameserver` 明文 223.5.5.5 | DoH（IP 形式） | 避免明文泄露节点域名 |
| `network_nodes.subscription_id` 可为 NULL（手动节点） | 必填；手动节点用“导入文件/粘贴 YAML” | 避免 NULL 唯一约束问题，路径统一 |
| 绑定 409 `profile_running` | 沿用现有 409 `conflict`（`ProfileLocked`） | 与现有 API 一致 |
| 权限不足 403 | 订阅/节点/地图需要管理员（403）；Profile 级接口无权限时 404（与现有约定一致） | 与代理库一致 |
| `row_version DEFAULT 1` | `DEFAULT 0` | 与项目一致 |
