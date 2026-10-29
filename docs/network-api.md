# /api/v1/network 接口

所有路径都在 `/api/v1/network` 下（`/api/network` 为兼容别名，不进 OpenAPI），沿用全局鉴权守卫、
`ApiResponse {success, message, data}` 包装和统一错误体
`{"error": {"code", "message", "details"}, "detail"}`。所有响应带 `Cache-Control: no-store`（含出口 IP）。
任何响应都不包含订阅 URL、节点密码/UUID、数据源密钥或密文。

权限：订阅、节点、检测、地图、设置、绑定写入需要**管理员**（否则 403）。Profile 级接口先检查该 Profile 的
访问权限（无权访问与不存在同样返回 404），绑定写入还需管理员。运行时查询需要 `view`，重连/打开检测网站需要 `launch`。

## 常用错误码

| HTTP | code | 含义 |
| --- | --- | --- |
| 404 | `not_found` | 订阅 / 节点 / 任务 / Profile 不存在 |
| 409 | `conflict` | Profile 正在运行或被其他实例 lease（绑定只能在关闭时修改） |
| 409 | `stale_write` | 版本已过期（绑定 `binding_row_version`、订阅 `row_version`） |
| 409 | `secrets_unavailable` | 未配置或无法使用加密密钥 |
| 409 | `subscription_bound` | 订阅仍有被绑定的节点；`details: [{"bound_profiles": N}]` |
| 409 | `probe_busy` | 已有批量检测在运行 |
| 409 | `not_running` | 该 Profile 没有运行中的浏览器 / 绑定网络 |
| 422 | `validation_error` | 请求体不合法 |
| 422 | 订阅错误码 | `https_required` `invalid_url` `blocked_address` `dns_failed` `timeout` `tls_failed` `http_status` `connection_failed` `too_large` `too_many_redirects` `unsupported_encoding` `invalid_encoding` `empty` `invalid_yaml` `yaml_too_complex` `no_proxies` `providers_only` `uri_list_unsupported` `base64_unsupported` `no_valid_proxies` `too_many_nodes` `file_required` `invalid_source` |
| 422 | `node_unavailable` | 节点已失效/无效，不能绑定 |
| 503 | `binary_missing` `binary_setuid` `binary_writable` `binary_owner` `binary_invalid` `binary_outdated` | Mihomo 不可用 |

启动接口 `POST /api/v1/profiles/{id}/launch`（原有）对绑定 Profile 新增：
`422 node_retired|node_invalid|node_missing`、`503 binary_*|engine_*`、`502 exit_check_failed|node_unreachable|node_timeout|node_tls_failed|node_auth_failed`、
`409 secrets_unavailable`。失败时不会启动浏览器、不会回退到手动代理或直连，lease 被释放。

## 状态与设置

```http
GET   /api/v1/network/status
GET   /api/v1/network/settings
PATCH /api/v1/network/settings
```

`status.data`：`engine {available, path, version | code, message}`、`geoip {available, name, size, modified_at}`、
`encryption_available`、`settings`、`active_runtimes`。

`PATCH` 字段（发送即生效，`null` 清除可空字段）：`mihomo_path`、`probe_concurrency`(1–32)、
`probe_timeout_seconds`(3–60)、`intel_cache_hours`(1–720)、`intel_ipinfo_enabled`、`intel_ippure_enabled`、
`intel_scamalytics_enabled`、`mask_exit_ips`、`intel_ipinfo_token`、`intel_scamalytics_url`(必须 https)、
`intel_scamalytics_key`。密钥只写，响应只给 `has_intel_*`。

## 订阅

```http
GET    /api/v1/network/subscriptions
POST   /api/v1/network/subscriptions                     201
GET    /api/v1/network/subscriptions/{id}
PATCH  /api/v1/network/subscriptions/{id}                {"name": "...", "row_version": 0}
POST   /api/v1/network/subscriptions/{id}/sync           {} | {"content": "<YAML>"}
DELETE /api/v1/network/subscriptions/{id}?retire_bound=false
```

创建（二选一）：

```json
{"name": "我的机场", "url": "https://sub.example.com/api/v1/client/subscribe?token=…"}
{"name": "本地配置", "content": "proxies:\n  - {name: JP, type: trojan, …}", "file_name": "clash.yaml"}
```

首次导入失败时**不会保存**任何东西，直接返回 422 和错误码。成功：

```json
{"success": true, "message": "Subscription imported", "data": {
  "subscription": {"id": "s…", "name": "我的机场", "source_kind": "url",
    "source_display": "https://sub.example.com/…", "status": "ok",
    "last_synced_at": "2026-10-29T03:51:59+00:00", "usage": {"upload": 1, "download": 2, "total": 100, "expire": 1893456000},
    "revision": 1, "row_version": 0, "node_counts": {"active": 42, "retired": 0, "invalid": 0}, "bound_profiles": 0},
  "report": {"created": 42, "updated": 0, "retired": 0, "unchanged": 0,
    "warnings": [{"index": 7, "reason": "unsupported protocol"}], "last_synced_at": "…"}}}
```

同步：URL 订阅从保存的 URL 重新拉取；文件订阅需要提供新的 `content`。同步失败时订阅变为 `status: error`，
已有节点不变。删除：有绑定且 `retire_bound=false` → 409 `subscription_bound`；`retire_bound=true` →
`{"deleted": false, "retired_bound_nodes": N}`。

## 节点与检测

```http
GET  /api/v1/network/nodes?subscription_id=&status=active|retired|invalid&country=JP&health=ok|error|unknown&search=&located=true|false&offset=0&limit=200
GET  /api/v1/network/nodes/{id}
POST /api/v1/network/nodes/{id}/check          {"include_intel": true, "refresh_intel": false}
POST /api/v1/network/nodes/check-batch         {"node_ids": ["n…", …]}        ≤200
GET  /api/v1/network/jobs/{job_id}
POST /api/v1/network/jobs/{job_id}/cancel
```

节点：

```json
{"id": "n…", "subscription_id": "s…", "subscription_name": "…", "name": "🇯🇵 东京 01", "protocol": "trojan",
 "entry": "jp.example.com:443", "status": "active", "profile_count": 1, "shared_exit_count": 2,
 "check": {"health": "ok", "exit_ip": "203.0.113.7", "country_iso": "JP", "city": null, "region": null,
   "latitude": 35.69, "longitude": 139.69, "timezone": "Asia/Tokyo", "precision": "coordinates",
   "location_source": "geoip", "latency_ms": 120, "error_code": null, "error": null,
   "checked_at": "…", "stale": false},
 "intel": {"ip": "203.0.113.7", "sources": {"ipinfo": {"status": "ok", "city": "Tokyo", "asn": 2516, "…": "…"},
   "ippure": {"status": "ok", "fraud_score": 12, "is_residential": true, "is_broadcast": false},
   "scamalytics": {"status": "not_configured"}},
   "composite": {"risk_score": 12, "purity_score": 88, "grade": "excellent",
     "score_sources": [{"source": "ippure", "score": 12}], "ip_type": "residential", "native": true,
     "flags": [], "signals": [{"level": "info", "code": "residential"}, {"level": "info", "code": "native"}],
     "location": {"city": "Tokyo", "region": "Tokyo", "country_iso": "JP", "timezone": "Asia/Tokyo",
       "latitude": 35.6895, "longitude": 139.6917}, "asn": 2516, "as_org": "KDDI CORPORATION", "hostname": null}},
 "intel_checked_at": "…"}
```

`precision`：`unknown` | `country` | `coordinates`（本地 GeoIP 坐标，近似）| `city`（GeoIP 无结果时由数据源落点）。
`signals[].code`：`datacenter residential broadcast native vpn proxy tor relay high_fraud ip_mismatch`。

批量任务：`{"id": "j…", "state": "queued|running|finished|failed|cancelled", "total": 3, "done": 3, "ok": 2,
"failed": 1, "skipped": 0, "engine_processes": 1, "error": null}`。`engine_processes` 是先后启动过的探测进程数
（只有 Mihomo 拒绝某个节点配置时才会大于 1，且始终顺序进行）。

## 地图

```http
GET /api/v1/network/map
```

```json
{"success": true, "data": {
  "features": {"type": "FeatureCollection", "features": [
    {"type": "Feature", "id": "n…", "geometry": {"type": "Point", "coordinates": [139.69, 35.69]},
     "properties": {"node_id": "n…", "label": "JP 01", "subscription_name": "…", "protocol": "trojan",
       "status": "active", "country_iso": "JP", "city": null, "region": null, "precision": "coordinates",
       "location_source": "geoip", "timezone": "Asia/Tokyo", "health": "ok", "latency_ms": 120,
       "exit_ip": "203.0.113.7", "checked_at": "…", "stale": false, "profile_count": 1,
       "shared_exit_count": 2, "purity_score": 88, "risk_score": 12, "grade": "excellent"}}]},
  "unknown_location": [{"node_id": "n…", "label": "…", "status": "active", "health": "error",
     "country_iso": null, "exit_ip": null}],
  "unknown_location_count": 8, "checked_count": 20, "total_nodes": 28}}
```

## 绑定

```http
GET    /api/v1/network/bindings
GET    /api/v1/network/bindings/{profile_id}
PUT    /api/v1/network/bindings/{profile_id}     {"node_id": "n…", "binding_row_version": null, "profile_row_version": 8}
DELETE /api/v1/network/bindings/{profile_id}?binding_row_version=N
```

- `binding_row_version`：当前没有绑定时必须为 `null`；有绑定时必须等于其 `row_version`，否则 409 `stale_write`。
- Profile 正在运行或被 lease → 409 `conflict`（不热切换）；Profile 在回收站或不存在 → 404；节点失效 → 422。
- 成功响应额外包含 `geography_conflict`（Profile 设置了固定时区/坐标，界面会询问是否用原有
  `POST /profiles/clear-geography` 恢复跟随出口；不会自动修改）与 `overrides_manual_proxy`（绑定期间手动代理不生效）。
- `GET /bindings` 每项附带 `runtime`。

## 运行时

```http
GET  /api/v1/network/runtime
GET  /api/v1/network/runtime/{profile_id}
POST /api/v1/network/runtime/{profile_id}/reconnect
POST /api/v1/network/runtime/{profile_id}/open-site      {"site": "ippure" | "ipip" | "ipinfo" | "scamalytics"}
```

运行时：`{"profile_id", "state": "stopped|ready|running|degraded|stopping", "adapter_kind": "native|mihomo",
"node_id", "node_name", "listen_port", "exit_ip", "engine_alive", "started_at", "error": {"code", "message"} | null}`。
只反映**本实例**的会话。`reconnect` 以同节点、同端口、同凭据重启引擎。`open-site` 在该 Profile 自己的浏览器中打开
固定白名单页面（Scamalytics 使用当前出口 IP），因此经该 Profile 的出口访问。
