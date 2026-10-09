# NeoHeberg 自动续期 + 开机守护

面板 `dash.neoheberg.fr` 的 LXC 容器每日自动续期与保活（GitHub Actions）。

## 功能

每天 UTC 02:00（北京时间 10:00）运行一次：

1. **固定 IP 代理** — 经节点出口登录（可选；未配置则直连）
2. **登录** — 无头 Chrome 打开登录页，自动解 Cap.js 验证码并提交账号密码
3. **续期** — 剩余天数 ≤ `RENEW_DAYS`（默认 14）时续期，+31 天且不损失已有天数
4. **开机守护** — 容器已停止则下发开机指令，45 秒后复验状态
5. **Telegram 通知** — 汇报本次动作、到期日、容器状态、代理状态

## 为什么用无头浏览器登录

面板登录带 **Cap.js 工作量证明验证码**。`POST /redeem` 会校验 `instr` 指纹字段——
服务端下发一段 base64 + deflate 的约 9KB 检测脚本，在沙箱 iframe 里采集
`navigator.webdriver` / 字体宽度 / `WebGL` 原生性 / `screen` 等信号，并检测
Node 环境泄漏。纯 HTTP 客户端无法作答（403 `missing_instrumentation_response`）。

无头 Chromium 能通过检测（检测针对的是 Node 运行时，不是无头浏览器引擎），
实测约 11 秒解出 `cap-token`。只有登录这一步需要浏览器，其余面板操作走纯 HTTP。

**不保存任何 cookie**：每次运行都重新登录，因此不依赖任何会过期的持久化凭证。

## 固定出口 IP（防风控）

面板会对「同一账号反复从不同机房 IP 登录」做风控标记。让每次运行的来源 IP
保持一致（固定节点出口），可以显著降低触发概率。

实现要点：

- 节点由 workflow 里的 `setup_proxy.sh` 拉起的 **sing-box** 提供，监听
  `127.0.0.1:1080`，导出 `PROXY_SERVER` / `IS_PROXY` 两个变量。
- `socks_proxy.py` 负责读取这两个变量并做两件事：
  - **HTTP 层** — 用 PySocks 的 `SocksiPyHandler` 构建 opener，`renew.py` 所有
    `urllib` 请求都走它；
  - **浏览器层** — 返回 `{"server": "socks5://…"}` 交给 Playwright 的 `proxy`
    参数，使无头 Chrome 流量也走固定出口。
- **不做全局 socket monkeypatch**：那会把 Playwright 到本地 driver 的连接一并
  劫持，导致浏览器启动失败。两条路径分开显式处理。
- 未配置 `NODE_LINK` 时静默退回直连 —— 功能可以先上线，secret 后补。

### 节点配置

`NODE_LINK` 是标准分享链接，`setup_proxy.sh` 会自动识别协议：

```
vless://<uuid>@<host>:<port>?type=tcp&security=reality&sni=...&pbk=...&fp=chrome#name
vmess://<base64 json>
trojan://<pw>@<host>:<port>?sni=...&type=ws&path=...
ss://<base64>@<host>:<port>
socks5://<host>:<port>
```

换节点只需改 `NODE_LINK` 这一个 secret，脚本与 workflow 都不用动。

> **注意**：`NODE_LINK` 含完整节点凭证（UUID、Reality 公钥、端口）。
> 本仓库是公开的，因此它必须放在 **Repository Secret**，绝不能写进代码或
> 提交到仓库。日志里只输出脱敏摘要（`vless @ 2a0***:22641`）。

## 配置

### Secrets

| Secret | 说明 |
|---|---|
| `NEO_USER` | 面板登录账号 |
| `NEO_PASSWORD` | 面板登录密码 |
| `NEO_VMID` | 容器实例 ID（面板服务列表里可见） |
| `NODE_LINK` | 节点分享链接（可选；配了就走固定 IP，不配直连） |
| `TG_BOT_TOKEN` | Telegram bot token（可选，不配则跳过通知） |
| `TG_CHAT_ID` | Telegram chat id（可选） |

### 本地测试

```bash
export NEO_USER=...  NEO_PASSWORD=...  NEO_VMID=...
export RENEW_DAYS=10
# 可选：走固定出口
export PROXY_SERVER=socks5://127.0.0.1:1080
python3 renew.py
```

只测验证码求解（不登录、不改任何状态）：

```bash
python3 renew.py --selftest
```

单独检查代理是否可用：

```bash
PROXY_SERVER=socks5://127.0.0.1:1080 python3 socks_proxy.py
```

工作流也支持手动触发 `selftest` 模式。

## 接口备忘

```
GET  /                                   面板 CSRF: var CSRF = "..."（或表单里的 csrf_token）
GET  /app/services/vps-stats.php?id=<id>  {"status":"running|stopped|starting|stopping|restarting",...}
POST /app/services/vps-power.php         vmid=<id>&signal=start|shutdown|reboot&csrf_token=...
POST /services/renew                     csrf_token=...&type=vps&id=<id>   → +31 天
```

登录为两步式 PHP 表单：先填 `identifier` 并点 `#goToPassword`，密码字段才会显示；
提交时带 `csrf_token` / `identifier` / `password` / `remember_me` / `cap-token`。
成功后会拿到 `__Host-NH` / `__Host-NH-Remember` cookie（后者 30 天）。

## 注意

- **停机 ≠ 网络故障**：容器关机后 IPv6 仍可能回 ping 但所有 TCP 端口不通，
  同网段邻居也不可达 —— 看起来像路由故障。判断容器状态要查面板
  `vps-stats.php` 的 `status` 字段，不要靠网络探测猜。
- 容器若装了自己的隧道守护（systemd timer），首次开机后隧道服务可能因网络未就绪
  而退出，需等其自愈。
- 续期为免费（面板显示 `offre gratuite`），余额 0 不影响。
- 日志会遮盖账号与实例 ID（`mask()`），因为本仓库公开、Actions 日志对外可见。
  节点信息只打印协议与脱敏地址，**不含 UUID / 密钥**。
