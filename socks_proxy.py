#!/usr/bin/env python3
"""
socks_proxy.py — 固定出口 IP 代理（NeoHeberg 续期任务用）

用途
----
面板对同一账号从不同机房 IP 反复登录会触发风控。走固定节点出口，
让每次运行的来源 IP 保持一致。

设计
----
* 依赖 PySocks（体积小、成熟）
* **不做全局 socket monkeypatch** —— 那会连 playwright 到本地 driver 的
  连接也一起劫持，导致浏览器启动失败。改为两条明确路径：
    - urllib 请求：build_opener(SocksiPyHandler)
    - playwright 浏览器：launch(proxy={...})
* 无 NODE_LINK / PROXY_SERVER 时静默退回直连
* NODE_LINK 只用于**节点摘要与自检**；实际转发由本地 sing-box 承担
  （workflow 里 setup_proxy.sh 拉起，监听 127.0.0.1:1080）

环境变量
--------
    NODE_LINK       分享链接（vless:// vmess:// ss:// trojan:// socks5://）
                    仅用于探测与日志摘要
    PROXY_SERVER    本地代理地址，如 socks5://127.0.0.1:1080
                    优先使用；未设置时探测 127.0.0.1:1080
    PROXY_REQUIRED  1/true 时代理不可用直接抛错，而非退回直连
"""
import os
import re
import json
import base64
import socket
import urllib.parse
import urllib.request

LOG = []


def _log(msg):
    line = f"[proxy] {msg}"
    print(line, flush=True)
    LOG.append(line)


def mask(s, show=2):
    """日志脱敏：只保留首尾各 show 位"""
    s = str(s or "")
    if not s:
        return "(空)"
    if len(s) <= show * 2:
        return "*" * len(s)
    return s[:show] + "*" * (len(s) - show * 2) + s[-show:]


# ───────────────────── 节点链接解析（仅探测/摘要） ─────────────────────

def parse_node_link(link):
    """解析分享链接 → dict，或 None。仅提取探测/摘要所需字段，不含凭证。"""
    link = (link or "").strip()
    if "://" not in link:
        return None
    proto = link.split("://", 1)[0].lower()
    body = link.split("://", 1)[1].split("#", 1)[0]
    try:
        if proto in ("socks5", "socks", "socks5h"):
            hp = body.split("@")[-1].split("/")[0]
            host, _, port = hp.rpartition(":")
            return {"proto": "socks5", "server": host, "port": int(port)}

        if proto == "vmess":
            pad = "=" * (-len(body) % 4)
            d = json.loads(base64.b64decode(body + pad).decode("utf-8", "ignore"))
            return {"proto": "vmess", "server": d.get("add", ""),
                    "port": int(d.get("port") or 443),
                    "transport": d.get("net", "tcp"),
                    "security": "tls" if d.get("tls") else "none",
                    "sni": d.get("sni") or d.get("host") or d.get("add", "")}

        if proto in ("vless", "trojan"):
            cred_host, _, query = body.partition("?")
            _, _, hostport = cred_host.rpartition("@")
            host, _, port = hostport.rpartition(":")
            q = urllib.parse.parse_qs(query)
            one = lambda k, d="": (q.get(k) or [d])[0]
            return {"proto": proto, "server": host, "port": int(port or 443),
                    "transport": one("type", "tcp"),
                    "security": one("security", "none"),
                    "sni": one("sni") or one("peer") or host}

        if proto == "ss":
            raw = body
            if "@" not in raw:
                pad = "=" * (-len(raw) % 4)
                raw = base64.b64decode(raw + pad).decode("utf-8", "ignore")
            _, _, hostport = raw.rpartition("@")
            host, _, port = hostport.rpartition(":")
            return {"proto": "ss", "server": host, "port": int(port)}
    except Exception as e:
        _log(f"节点链接解析失败 ({proto}): {type(e).__name__}")
        return None
    return None


def describe_node(link):
    """可安全打印的节点摘要（脱敏，不含 UUID/密钥）"""
    n = parse_node_link(link)
    if not n:
        return "未配置节点"
    return (f"{n['proto']} @ {mask(n['server'], 3)}:{n['port']}"
            f" transport={n.get('transport', '-')}"
            f" security={n.get('security', '-')}")


# ───────────────────── 代理启用 ─────────────────────

_STATE = {"enabled": False, "endpoint": "", "why": "未初始化"}
_OPENER = None


def _local_endpoint():
    """返回可用本地代理 (host, port)，或 None"""
    ps = (os.environ.get("PROXY_SERVER") or "").strip()
    if ps:
        m = re.match(r"^(?:socks5h?|socks)://([^:/]+):(\d+)/?$", ps, re.I)
        if m:
            return m.group(1), int(m.group(2))
        _log(f"⚠️ PROXY_SERVER 格式无法识别（期望 socks5://host:port）")
    for port in (1080, 1081):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            _log(f"ℹ️ 探测到本地代理端口 127.0.0.1:{port}")
            return "127.0.0.1", port
        except OSError:
            continue
    return None


def _probe(host, port, timeout=20):
    """经代理请求 api.ipify.org，返回出口 IP 字符串"""
    if not _OPENER:
        raise RuntimeError("opener 未构建")
    with _OPENER.open("https://api.ipify.org", timeout=timeout) as r:
        return r.read().decode().strip()


def init(required=None):
    """启用代理。返回 True 表示已启用。

    required=None 时读 PROXY_REQUIRED（默认 false）。
    不修改全局 socket —— 调用方需显式使用 get_opener() / playwright_proxy()。
    """
    global _OPENER
    if required is None:
        required = os.environ.get("PROXY_REQUIRED", "false").lower() in ("1", "true", "yes")

    link = (os.environ.get("NODE_LINK") or "").strip()
    ep = _local_endpoint()

    if not ep:
        if link:
            _log(f"⚠️ NODE_LINK 已配置但本地无代理监听：{describe_node(link)}")
            _log("   提示：workflow 需先跑 setup_proxy.sh 拉起 sing-box")
        msg = "未检测到本地代理，直连模式"
        if required:
            raise RuntimeError(msg + "（PROXY_REQUIRED=1）")
        _log(f"ℹ️ {msg}")
        _STATE.update(enabled=False, endpoint="", why=msg)
        return False

    try:
        import socks
        import sockshandler
    except ImportError:
        msg = "PySocks/sockshandler 不可用（pip install pysocks）"
        if required:
            raise RuntimeError(msg)
        _log(f"⚠️ {msg} → 退回直连")
        _STATE.update(enabled=False, endpoint="", why=msg)
        return False

    host, port = ep
    try:
        _OPENER = urllib.request.build_opener(
            sockshandler.SocksiPyHandler(socks.SOCKS5, host, port, rdns=True))
        ip = _probe(host, port)
    except Exception as e:
        msg = f"代理不可用（{type(e).__name__}: {str(e)[:80]}）"
        if required:
            raise RuntimeError(msg)
        _log(f"⚠️ {msg} → 退回直连")
        _STATE.update(enabled=False, endpoint="", why=msg)
        _OPENER = None
        return False

    _STATE.update(enabled=True, endpoint=f"{host}:{port}", why="ok")
    _log(f"✅ 代理已启用（socks5 {host}:{port}，出口 {mask(ip, 3)}）")
    return True


def get_opener():
    """返回走代理的 opener；未启用则返回默认 opener"""
    return _OPENER or urllib.request.build_opener()


def playwright_proxy():
    """playwright launch/new_context 用的 proxy 参数；未启用返回 None"""
    if not _STATE["enabled"]:
        return None
    return {"server": f"socks5://{_STATE['endpoint']}"}


def enabled():
    return _STATE["enabled"]


def describe():
    """供日志 / TG 通知使用的脱敏摘要"""
    if _STATE["enabled"]:
        link = (os.environ.get("NODE_LINK") or "").strip()
        extra = f"，节点 {describe_node(link)}" if link else ""
        return f"已启用（{_STATE['endpoint']}{extra}）"
    return f"未启用（{_STATE['why']}）"


def egress_ip(timeout=20):
    """经代理查询出口 IP；失败返回 None（调用方负责脱敏）"""
    if not _STATE["enabled"]:
        return None
    try:
        host, port = _STATE["endpoint"].split(":")
        return _probe(host, int(port), timeout)
    except Exception:
        return None


if __name__ == "__main__":
    ok = init()
    print("状态:", describe())
    if ok:
        ip = egress_ip()
        print("出口 IP:", mask(ip, 3) if ip else "查询失败")
