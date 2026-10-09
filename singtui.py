#!/usr/bin/env python3
"""
SingTUI — dual-pane multi-instance manager for sing-box

Written by AI (Grok, built by xAI).

  a Add (from template)   b Import from clipboard (URI or JSON)
  e Edit   s Start   x Stop   r Restart   n Enable   m Disable
  p Ping selected (must be up)   P Ping all up
  g Geo (must be up)   d Remove   q Quit

Ping/Geo only when proxy is up; measured through local mixed inbound.
Cache TTL for ping/geo results: 1800 seconds (30 min).
"""

import base64
import curses
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

HOME = Path.home()
BASE_DIR = HOME / ".config" / "singtui"
CONFIG_DIR = BASE_DIR / "configs"
CACHE_PATH = BASE_DIR / "cache.json"
SERVICE_DIR = HOME / ".config" / "systemd" / "user"
SERVICE_PREFIX = "singtui-"

BASE_PORT = 2081
CACHE_TTL = 60*30  # seconds

# Built-in skeleton for "Add". Add any outbound protocol sing-box supports
# (shadowsocks, vmess, vless, trojan, hysteria2, tuic, …) via Edit (e)
# or Import (b). See https://sing-box.sagernet.org/configuration/outbound/
DEFAULT_TEMPLATE = {
    "log": {"level": "info", "timestamp": True},
    "inbounds": [
        {
            "type": "mixed",
            "tag": "mixed-in",
            "listen": "127.0.0.1",
            "listen_port": 2081,
        }
    ],
    "outbounds": [
        {"type": "direct", "tag": "direct"},
        {"type": "block", "tag": "block"},
    ],
    "route": {
        "rules": [
            {"ip_is_private": True, "outbound": "direct"},
            {
                "domain_suffix": ["local", "localhost", "lan"],
                "outbound": "direct",
            },
        ],
        "final": "direct",
        "auto_detect_interface": True,
    },
}

ACTIONS = [
    ("Add",     "a", "add"),
    ("Import",  "b", "import"),
    ("Edit",    "e", "edit"),
    ("Start",   "s", "start"),
    ("Stop",    "x", "stop"),
    ("Restart", "r", "restart"),
    ("Enable",  "n", "enable"),
    ("Disable", "m", "disable"),
    ("Ping",    "p", "ping"),
    ("Geo",     "g", "geo"),
    ("Remove",  "d", "remove"),
    ("Quit",    "q", "quit"),
]


# ── helpers ────────────────────────────────────────────────────────────────

def ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE_DIR.mkdir(parents=True, exist_ok=True)


def list_configs():
    return sorted(p.name for p in CONFIG_DIR.glob("config-*.json"))


def config_number(name):
    m = re.match(r"config-(\d+)\.json$", name)
    return int(m.group(1)) if m else None


def next_config_name():
    nums = [n for n in (config_number(c) for c in list_configs()) if n is not None]
    return f"config-{max(nums, default=0) + 1}.json"


def port_for_config(name):
    n = config_number(name)
    return BASE_PORT + (n - 1) if n else BASE_PORT


def config_path(name):
    return CONFIG_DIR / name


def load_config(name):
    return json.loads(config_path(name).read_text())


def set_listen_port(data, port):
    for ib in data.get("inbounds", []):
        if "listen_port" in ib or ib.get("type") in (
            "mixed", "http", "socks", "tun", "redirect", "tproxy"
        ):
            ib["listen_port"] = port
            break
    else:
        data.setdefault("inbounds", []).insert(
            0,
            {"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": port},
        )
    return data


def get_listen_port(name):
    try:
        for ib in load_config(name).get("inbounds", []):
            if "listen_port" in ib:
                return int(ib["listen_port"])
    except Exception:
        pass
    return None


def short_name(name):
    return name.replace(".json", "")


def outbound_endpoint(name):
    """Return 'server:port' of first remote outbound, or '?'."""
    servers = []
    try:
        data = load_config(name)
        for ob in data.get("outbounds", []):
            host = ob.get("server") or ob.get("server_name") or ob.get("address")
            if not host or host in ("127.0.0.1", "localhost", "::1"):
                continue
            port = ob.get("server_port")
            if port:
                return f"{host}:{port}"
            return str(host)
    except Exception:
        pass
    return "?"



def write_new_config(data: dict) -> str:
    """Assign next name + port, write file, install unit. Returns filename."""
    ensure_dirs()
    name = next_config_name()
    port = port_for_config(name)
    data = set_listen_port(data, port)
    # ensure minimal log if missing
    data.setdefault("log", {"level": "info", "timestamp": True})
    config_path(name).write_text(json.dumps(data, indent=2) + "\n")
    install_unit(name)
    return name


def add_config():
    """Create new config from built-in DEFAULT_TEMPLATE."""
    ensure_dirs()
    data = json.loads(json.dumps(DEFAULT_TEMPLATE))  # deep copy
    return write_new_config(data)


# ── clipboard ──────────────────────────────────────────────────────────────

def read_clipboard() -> str:
    """Try wl-paste, xclip, xsel, pbpaste."""
    commands = [
        ["wl-paste", "-n"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
        ["pbpaste"],
    ]
    for cmd in commands:
        if not shutil.which(cmd[0]):
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            continue
    return ""


def _b64decode(s: str) -> str:
    s = s.strip()
    pad = (-len(s)) % 4
    if pad:
        s += "=" * pad
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return decoder(s).decode("utf-8", errors="replace")
        except Exception:
            continue
    raise ValueError("invalid base64")


def _base_config(outbound: dict) -> dict:
    """Wrap a single outbound into a full sing-box config."""
    return {
        "log": {"level": "info", "timestamp": True},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": 2081,
            }
        ],
        "outbounds": [
            outbound,
            {"type": "direct", "tag": "direct"},
        ],
        "route": {
            "rules": [
                {"protocol": "dns", "outbound": "direct"},
                {"ip_is_private": True, "outbound": "direct"},
                {
                    "domain_suffix": ["local", "localhost", "lan"],
                    "outbound": "direct",
                },
            ],
            "final": "proxy",
            "auto_detect_interface": True,
        },
    }


def _parse_host_port(netloc: str):
    """Return (host, port) from netloc (may include userinfo already stripped)."""
    netloc = netloc.strip()
    if netloc.startswith("["):  # IPv6 [addr]:port
        m = re.match(r"\[([^\]]+)\]:(\d+)$", netloc)
        if not m:
            raise ValueError(f"bad IPv6 host:port: {netloc}")
        return m.group(1), int(m.group(2))
    if ":" not in netloc:
        raise ValueError(f"missing port: {netloc}")
    host, port_s = netloc.rsplit(":", 1)
    # strip path/query leftovers
    if "/" in host:
        host = host.split("/")[0]
    if "?" in port_s:
        port_s = port_s.split("?")[0]
    return host.strip("[]"), int(port_s)


def _qs(query: str) -> dict:
    """Parse query string to lowercase-key dict (first value only)."""
    out = {}
    for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items():
        out[k.lower()] = v[0] if v else ""
    return out


def _tls_from_params(p: dict, default_enabled=False) -> Optional[dict]:
    """Build sing-box tls block from share-link query params."""
    security = (p.get("security") or "").lower()
    sni = p.get("sni") or p.get("peer") or p.get("host") or ""
    fp = p.get("fp") or p.get("fingerprint") or ""
    alpn_raw = p.get("alpn") or ""
    insecure = p.get("insecure") in ("1", "true", "yes")
    allow_insecure = p.get("allowinsecure") in ("1", "true", "yes") or insecure

    enabled = default_enabled or security in ("tls", "reality") or bool(sni) or bool(fp)
    if security in ("none", "0") and not default_enabled:
        enabled = False
    if not enabled and not allow_insecure:
        return None

    tls: dict = {"enabled": True}
    if sni:
        tls["server_name"] = sni
    if allow_insecure:
        tls["insecure"] = True
    if alpn_raw:
        tls["alpn"] = [a.strip() for a in alpn_raw.split(",") if a.strip()]
    if fp:
        tls["utls"] = {"enabled": True, "fingerprint": fp}
    if security == "reality":
        reality = {"enabled": True}
        if p.get("pbk"):
            reality["public_key"] = p["pbk"]
        if p.get("sid"):
            reality["short_id"] = p["sid"]
        if p.get("spx"):
            reality["spider_x"] = urllib.parse.unquote(p["spx"])
        tls["reality"] = reality
        if not fp:
            tls["utls"] = {"enabled": True, "fingerprint": "chrome"}
    return tls


def _transport_from_params(p: dict) -> Optional[dict]:
    """Build sing-box transport from type/net/path/host/serviceName."""
    net = (p.get("type") or p.get("net") or "tcp").lower()
    if net in ("tcp", "none", ""):
        return None
    if net == "ws":
        t: dict = {"type": "ws"}
        path = p.get("path") or "/"
        t["path"] = urllib.parse.unquote(path)
        host = p.get("host") or ""
        if host:
            t["headers"] = {"Host": host}
        return t
    if net == "grpc":
        t = {"type": "grpc"}
        sn = p.get("servicename") or p.get("service_name") or p.get("path") or ""
        if sn:
            t["service_name"] = urllib.parse.unquote(sn)
        return t
    if net in ("http", "h2"):
        t = {"type": "http"}
        path = p.get("path") or ""
        if path:
            t["path"] = urllib.parse.unquote(path)
        host = p.get("host") or ""
        if host:
            t["host"] = [host]
        return t
    if net == "httpupgrade":
        t = {"type": "httpupgrade"}
        path = p.get("path") or "/"
        t["path"] = urllib.parse.unquote(path)
        host = p.get("host") or ""
        if host:
            t["host"] = host
        return t
    if net == "quic":
        return {"type": "quic"}
    return None


def parse_ss_url(url: str) -> dict:
    """
    ss:// SIP002 / legacy → sing-box shadowsocks outbound.
      ss://BASE64(method:password)@host:port#name
      ss://BASE64(method:password@host:port)#name
    """
    url = url.strip()
    if not url.lower().startswith("ss://"):
        raise ValueError("not an ss:// URL")

    body = url[5:]
    if "#" in body:
        body, _frag = body.split("#", 1)
    body = body.strip()
    # drop query (plugins) for core fields
    query = ""
    if "?" in body:
        body, query = body.split("?", 1)

    method = password = host = None
    port = None

    if "@" in body:
        userinfo, hostport = body.rsplit("@", 1)
        try:
            decoded = _b64decode(userinfo)
        except ValueError:
            decoded = urllib.parse.unquote(userinfo)
        if ":" not in decoded:
            raise ValueError("ss userinfo missing method:password")
        method, password = decoded.split(":", 1)
        host, port = _parse_host_port(urllib.parse.unquote(hostport))
    else:
        decoded = _b64decode(body)
        if "@" not in decoded:
            raise ValueError("legacy ss decode failed")
        userinfo, hostport = decoded.rsplit("@", 1)
        method, password = userinfo.split(":", 1)
        host, port = _parse_host_port(hostport)

    if not method or not password or not host or not port:
        raise ValueError("incomplete ss fields")

    outbound = {
        "type": "shadowsocks",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "method": method,
        "password": password,
    }
    # optional plugin from query
    p = _qs(query)
    if p.get("plugin"):
        outbound["plugin"] = p["plugin"]
        if p.get("plugin-opts") or p.get("plugin_opts"):
            outbound["plugin_opts"] = p.get("plugin-opts") or p.get("plugin_opts")
    return _base_config(outbound)


def parse_vmess_url(url: str) -> dict:
    """vmess://BASE64(json) → sing-box vmess outbound."""
    url = url.strip()
    if not url.lower().startswith("vmess://"):
        raise ValueError("not a vmess:// URL")
    raw = url[8:]
    if "#" in raw:
        raw = raw.split("#", 1)[0]
    data = json.loads(_b64decode(raw))
    host = data.get("add") or data.get("host") or ""
    port = int(data.get("port") or 0)
    uuid = data.get("id") or ""
    if not host or not port or not uuid:
        raise ValueError("vmess missing add/port/id")

    outbound: dict = {
        "type": "vmess",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "uuid": uuid,
        "security": data.get("scy") or data.get("security") or "auto",
        "alter_id": int(data.get("aid") or 0),
    }
    # transport
    net = (data.get("net") or "tcp").lower()
    path = data.get("path") or ""
    host_hdr = data.get("host") or ""
    if net == "ws":
        t = {"type": "ws", "path": path or "/"}
        if host_hdr:
            t["headers"] = {"Host": host_hdr}
        outbound["transport"] = t
    elif net == "grpc":
        t = {"type": "grpc"}
        if path:
            t["service_name"] = path
        outbound["transport"] = t
    elif net in ("http", "h2"):
        t = {"type": "http"}
        if path:
            t["path"] = path
        if host_hdr:
            t["host"] = [host_hdr]
        outbound["transport"] = t
    # tls
    tls_flag = (data.get("tls") or "").lower()
    sni = data.get("sni") or host_hdr or ""
    fp = data.get("fp") or ""
    if tls_flag in ("tls", "1", "true") or sni or fp:
        tls: dict = {"enabled": True}
        if sni:
            tls["server_name"] = sni
        if fp:
            tls["utls"] = {"enabled": True, "fingerprint": fp}
        alpn = data.get("alpn") or ""
        if alpn:
            tls["alpn"] = [a.strip() for a in alpn.split(",") if a.strip()]
        outbound["tls"] = tls
    return _base_config(outbound)


def parse_vless_url(url: str) -> dict:
    """vless://uuid@host:port?params#name → sing-box vless outbound."""
    u = urllib.parse.urlparse(url.strip())
    if u.scheme.lower() != "vless":
        raise ValueError("not a vless:// URL")
    uuid = urllib.parse.unquote(u.username or "")
    host = u.hostname or ""
    port = u.port
    if not uuid or not host or not port:
        raise ValueError("vless missing uuid/host/port")
    p = _qs(u.query)

    outbound: dict = {
        "type": "vless",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "uuid": uuid,
    }
    if p.get("flow"):
        outbound["flow"] = p["flow"]
    if p.get("encryption") and p["encryption"] != "none":
        outbound["packet_encoding"] = p.get("packetencoding") or p.get("packet_encoding") or ""
    elif p.get("packetencoding") or p.get("packet_encoding"):
        outbound["packet_encoding"] = p.get("packetencoding") or p.get("packet_encoding")

    tls = _tls_from_params(p)
    if tls:
        outbound["tls"] = tls
    transport = _transport_from_params(p)
    if transport:
        outbound["transport"] = transport
    return _base_config(outbound)


def parse_trojan_url(url: str) -> dict:
    """trojan://password@host:port?params#name → sing-box trojan outbound."""
    u = urllib.parse.urlparse(url.strip())
    if u.scheme.lower() != "trojan":
        raise ValueError("not a trojan:// URL")
    password = urllib.parse.unquote(u.username or "")
    host = u.hostname or ""
    port = u.port
    if not password or not host or not port:
        raise ValueError("trojan missing password/host/port")
    p = _qs(u.query)

    outbound: dict = {
        "type": "trojan",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "password": password,
    }
    # trojan defaults to TLS
    tls = _tls_from_params(p, default_enabled=True)
    if tls:
        outbound["tls"] = tls
    transport = _transport_from_params(p)
    if transport:
        outbound["transport"] = transport
    return _base_config(outbound)


def parse_hysteria2_url(url: str) -> dict:
    """hysteria2:// / hy2:// password@host:port?params → sing-box hysteria2."""
    u = urllib.parse.urlparse(url.strip())
    if u.scheme.lower() not in ("hysteria2", "hy2"):
        raise ValueError("not a hysteria2:// URL")
    password = urllib.parse.unquote(u.username or "")
    host = u.hostname or ""
    port = u.port
    if not host or not port:
        raise ValueError("hysteria2 missing host/port")
    p = _qs(u.query)
    if not password:
        password = p.get("auth") or p.get("password") or ""

    outbound: dict = {
        "type": "hysteria2",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "password": password,
    }
    if p.get("up") or p.get("upmbps"):
        try:
            outbound["up_mbps"] = int(p.get("up") or p.get("upmbps"))
        except ValueError:
            pass
    if p.get("down") or p.get("downmbps"):
        try:
            outbound["down_mbps"] = int(p.get("down") or p.get("downmbps"))
        except ValueError:
            pass
    obfs = p.get("obfs") or ""
    if obfs and obfs not in ("none",):
        outbound["obfs"] = {
            "type": obfs,
            "password": p.get("obfs-password") or p.get("obfs_password") or "",
        }
    tls = _tls_from_params(p, default_enabled=True)
    if tls:
        outbound["tls"] = tls
    return _base_config(outbound)


def parse_hysteria_url(url: str) -> dict:
    """hysteria:// (v1) host:port?auth=… → sing-box hysteria outbound."""
    u = urllib.parse.urlparse(url.strip())
    if u.scheme.lower() != "hysteria":
        raise ValueError("not a hysteria:// URL")
    host = u.hostname or ""
    port = u.port
    if not host or not port:
        raise ValueError("hysteria missing host/port")
    p = _qs(u.query)
    auth = p.get("auth") or urllib.parse.unquote(u.username or "") or ""

    outbound: dict = {
        "type": "hysteria",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "auth_str": auth,
        "up_mbps": int(p.get("upmbps") or p.get("up") or 10),
        "down_mbps": int(p.get("downmbps") or p.get("down") or 50),
    }
    if p.get("obfs"):
        outbound["obfs"] = p["obfs"]
    tls = _tls_from_params(p, default_enabled=True)
    if tls:
        # hysteria v1 uses peer as SNI often
        if p.get("peer") and "server_name" not in tls:
            tls["server_name"] = p["peer"]
        outbound["tls"] = tls
    return _base_config(outbound)


def parse_tuic_url(url: str) -> dict:
    """tuic://uuid:password@host:port?params → sing-box tuic outbound."""
    u = urllib.parse.urlparse(url.strip())
    if u.scheme.lower() != "tuic":
        raise ValueError("not a tuic:// URL")
    user = urllib.parse.unquote(u.username or "")
    password = urllib.parse.unquote(u.password or "")
    # sometimes uuid:password in username only
    if ":" in user and not password:
        user, password = user.split(":", 1)
    host = u.hostname or ""
    port = u.port
    if not user or not host or not port:
        raise ValueError("tuic missing uuid/host/port")
    p = _qs(u.query)
    if not password:
        password = p.get("password") or ""

    outbound: dict = {
        "type": "tuic",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "uuid": user,
        "password": password,
    }
    if p.get("congestion_control") or p.get("congestion"):
        outbound["congestion_control"] = p.get("congestion_control") or p.get("congestion")
    if p.get("udp_relay_mode") or p.get("udp-relay-mode"):
        outbound["udp_relay_mode"] = p.get("udp_relay_mode") or p.get("udp-relay-mode")
    if p.get("alpn"):
        # alpn goes in tls
        pass
    tls = _tls_from_params(p, default_enabled=True)
    if tls:
        outbound["tls"] = tls
    return _base_config(outbound)


def parse_anytls_url(url: str) -> dict:
    """anytls://password@host:port?params → sing-box anytls outbound."""
    u = urllib.parse.urlparse(url.strip())
    if u.scheme.lower() != "anytls":
        raise ValueError("not an anytls:// URL")
    password = urllib.parse.unquote(u.username or "")
    host = u.hostname or ""
    port = u.port
    if not password or not host or not port:
        raise ValueError("anytls missing password/host/port")
    p = _qs(u.query)
    outbound: dict = {
        "type": "anytls",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "password": password,
    }
    tls = _tls_from_params(p, default_enabled=True)
    if tls:
        outbound["tls"] = tls
    return _base_config(outbound)


def parse_socks_url(url: str) -> dict:
    """socks:// or socks5:// [user:pass@]host:port → sing-box socks outbound."""
    u = urllib.parse.urlparse(url.strip())
    if u.scheme.lower() not in ("socks", "socks5", "socks4"):
        raise ValueError("not a socks:// URL")
    host = u.hostname or ""
    port = u.port or 1080
    if not host:
        raise ValueError("socks missing host")
    outbound: dict = {
        "type": "socks",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "version": "5" if u.scheme.lower() != "socks4" else "4",
    }
    if u.username:
        outbound["username"] = urllib.parse.unquote(u.username)
    if u.password:
        outbound["password"] = urllib.parse.unquote(u.password)
    return _base_config(outbound)


def parse_http_proxy_url(url: str) -> dict:
    """http:// or https:// user:pass@host:port as HTTP CONNECT proxy outbound."""
    u = urllib.parse.urlparse(url.strip())
    if u.scheme.lower() not in ("http", "https"):
        raise ValueError("not an http(s) proxy URL")
    host = u.hostname or ""
    port = u.port or (443 if u.scheme.lower() == "https" else 80)
    if not host:
        raise ValueError("http proxy missing host")
    outbound: dict = {
        "type": "http",
        "tag": "proxy",
        "server": host,
        "server_port": port,
    }
    if u.username:
        outbound["username"] = urllib.parse.unquote(u.username)
    if u.password:
        outbound["password"] = urllib.parse.unquote(u.password)
    if u.scheme.lower() == "https":
        outbound["tls"] = {"enabled": True}
    return _base_config(outbound)


# scheme → parser (first match wins for multi-link clipboard)
_URI_PARSERS = [
    (r"ss://[^\s\"'<>]+", parse_ss_url),
    (r"vmess://[^\s\"'<>]+", parse_vmess_url),
    (r"vless://[^\s\"'<>]+", parse_vless_url),
    (r"trojan://[^\s\"'<>]+", parse_trojan_url),
    (r"hysteria2://[^\s\"'<>]+", parse_hysteria2_url),
    (r"hy2://[^\s\"'<>]+", parse_hysteria2_url),
    (r"hysteria://[^\s\"'<>]+", parse_hysteria_url),
    (r"tuic://[^\s\"'<>]+", parse_tuic_url),
    (r"anytls://[^\s\"'<>]+", parse_anytls_url),
    (r"socks5?://[^\s\"'<>]+", parse_socks_url),
    (r"https?://[^\s\"'<>]+", parse_http_proxy_url),
]


def parse_clipboard_content(text: str) -> dict:
    """
    Return a sing-box config dict from clipboard text.
    Accepts share URIs (ss/vmess/vless/trojan/hy2/tuic/…) or JSON config.
    """
    text = text.strip()
    if not text:
        raise ValueError("clipboard empty")

    for pattern, parser in _URI_PARSERS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return parser(m.group(0))

    # JSON sing-box config
    if text.startswith("{") or text.startswith("["):
        data = json.loads(text)
        if isinstance(data, list):
            raise ValueError("JSON array not supported; need a config object")
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        if "outbounds" not in data and "inbounds" not in data:
            raise ValueError("JSON missing outbounds/inbounds")
        return data

    raise ValueError(
        "unsupported: need share URI "
        "(ss/vmess/vless/trojan/hysteria2/hy2/tuic/anytls/socks) or sing-box JSON"
    )


def import_from_clipboard() -> str:
    """Read clipboard → new config file. Returns filename."""
    text = read_clipboard()
    if not text:
        raise ValueError("clipboard empty (need xclip/wl-paste/xsel)")
    data = parse_clipboard_content(text)
    return write_new_config(data)


# ── systemd ────────────────────────────────────────────────────────────────

def unit_name(config):
    return f"{SERVICE_PREFIX}{short_name(config)}.service"


def unit_path(config):
    return SERVICE_DIR / unit_name(config)


def install_unit(config):
    ensure_dirs()
    cfg = config_path(config)
    singbox = shutil.which("sing-box") or "/usr/local/bin/sing-box"
    unit_path(config).write_text(
        f"[Unit]\nDescription=SingTUI ({config})\n"
        f"After=network-online.target\nWants=network-online.target\n\n"
        f"[Service]\nType=simple\nExecStart={singbox} run -c {cfg}\n"
        f"Restart=on-failure\nRestartSec=5\nLimitNOFILE=65535\n\n"
        f"[Install]\nWantedBy=default.target\n"
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return True


def remove_unit(config):
    uname = unit_name(config)
    subprocess.run(["systemctl", "--user", "stop", uname], capture_output=True, timeout=15)
    subprocess.run(["systemctl", "--user", "disable", uname], capture_output=True, timeout=10)
    p = unit_path(config)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)


def service_status(config):
    if not unit_path(config).exists():
        return "—"
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", unit_name(config)],
            capture_output=True, text=True, timeout=5,
        )
        state = (r.stdout or "").strip() or "?"
        return {"active": "up", "inactive": "down", "failed": "fail", "activating": "…"}.get(
            state, state[:6]
        )
    except Exception:
        return "?"


def service_enabled(config):
    """Return 'on' if unit is enabled, 'off' otherwise (or '—' if no unit)."""
    if not unit_path(config).exists():
        return "—"
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-enabled", unit_name(config)],
            capture_output=True, text=True, timeout=5,
        )
        state = (r.stdout or "").strip()
        # enabled / enabled-runtime / static / alias → treat enabled-ish as on
        if state in ("enabled", "enabled-runtime"):
            return "on"
        return "off"
    except Exception:
        return "?"


def service_action(config, action):
    """
    start/stop/restart — one-shot runtime control.
    enable/disable — persist across login (systemd user unit).
    """
    if not unit_path(config).exists():
        try:
            install_unit(config)
        except Exception as e:
            return False, str(e)
    uname = unit_name(config)
    try:
        r = subprocess.run(
            ["systemctl", "--user", action, uname],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            verbs = {
                "start": "started",
                "stop": "stopped",
                "restart": "restarted",
                "enable": "enabled",
                "disable": "disabled",
            }
            return True, f"{short_name(config)} {verbs.get(action, action)}"
        return False, (r.stderr or r.stdout or f"{action} failed").strip()
    except Exception as e:
        return False, str(e)



def remove_config(config):
    try:
        remove_unit(config)
    except Exception as e:
        return False, str(e)
    path = config_path(config)
    if path.exists():
        try:
            path.unlink()
        except OSError as e:
            return False, str(e)
    cache = _load_cache()
    for key in list(cache.keys()):
        if key.startswith(f"ping:{config}") or key.startswith(f"geo:{config}"):
            del cache[key]
    _save_cache(cache)
    return True, f"removed {config}"


# ── cache ──────────────────────────────────────────────────────────────────

def _load_cache():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_cache(data):
    try:
        CACHE_PATH.write_text(json.dumps(data, indent=2) + "\n")
    except Exception:
        pass


def cache_get(key):
    data = _load_cache()
    entry = data.get(key)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > CACHE_TTL:
        return None
    return entry


def cache_set(key, **fields):
    data = _load_cache()
    data[key] = {"ts": time.time(), **fields}
    _save_cache(data)


def cached_ping(name):
    e = cache_get(f"ping:{name}")
    return e["display"] if e else ""


def cached_geo(name):
    e = cache_get(f"geo:{name}")
    return e["display"] if e else ""


def clear_config_cache(cfg):
    data = _load_cache()
    for k in (f"ping:{cfg}", f"geo:{cfg}"):
        data.pop(k, None)
    _save_cache(data)


# ── ping / geo via local proxy ─────────────────────────────────────────────

def proxy_url(listen_port):
    return f"http://127.0.0.1:{listen_port}"


def ping_via_proxy(listen_port, timeout=8):
    url = "http://1.1.1.1/cdn-cgi/trace"
    proxy = proxy_url(listen_port)
    curl = shutil.which("curl")
    if curl:
        try:
            t0 = time.monotonic()
            r = subprocess.run(
                [curl, "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                 "-x", proxy, "--max-time", str(timeout), url],
                capture_output=True, text=True, timeout=timeout + 3,
            )
            ms = (time.monotonic() - t0) * 1000
            code = (r.stdout or "").strip()
            if r.returncode == 0 and code and code[0] in "123":
                return True, f"{ms:.0f}ms"
            t0 = time.monotonic()
            r = subprocess.run(
                [curl, "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                 "-x", proxy, "--max-time", str(timeout), "http://cp.cloudflare.com/"],
                capture_output=True, text=True, timeout=timeout + 3,
            )
            ms = (time.monotonic() - t0) * 1000
            code = (r.stdout or "").strip()
            if r.returncode == 0 and code:
                return True, f"{ms:.0f}ms"
            return False, "fail"
        except subprocess.TimeoutExpired:
            return False, "timeout"
        except Exception:
            return False, "err"
    try:
        t0 = time.monotonic()
        handlers = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handlers)
        req = urllib.request.Request(
            "http://cp.cloudflare.com/", headers={"User-Agent": "singbox-tui/1.0"}
        )
        with opener.open(req, timeout=timeout) as resp:
            resp.read(64)
        return True, f"{(time.monotonic() - t0) * 1000:.0f}ms"
    except Exception:
        return False, "fail"


def geo_via_proxy(listen_port, timeout=10):
    proxy = proxy_url(listen_port)
    url = "http://ip-api.com/json/?fields=status,countryCode,country"
    curl = shutil.which("curl")
    try:
        if curl:
            r = subprocess.run(
                [curl, "-sS", "-x", proxy, "--max-time", str(timeout), url],
                capture_output=True, text=True, timeout=timeout + 3,
            )
            if r.returncode != 0:
                return False, "?"
            data = json.loads(r.stdout)
        else:
            handlers = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            opener = urllib.request.build_opener(handlers)
            req = urllib.request.Request(url, headers={"User-Agent": "singbox-tui/1.0"})
            with opener.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
        if data.get("status") != "success":
            return False, "?"
        return True, data.get("countryCode") or data.get("country") or "?"
    except Exception:
        return False, "?"


def do_ping(name):
    port = get_listen_port(name)
    if not port:
        display = "no-port"
        cache_set(f"ping:{name}", display=display, ok=False)
        return display
    ok, msg = ping_via_proxy(port)
    display = msg if ok else f"✗{msg}"
    cache_set(f"ping:{name}", display=display, ok=ok)
    return display


def do_geo(name):
    port = get_listen_port(name)
    if not port:
        display = "—"
        cache_set(f"geo:{name}", display=display, ok=False)
        return display
    ok, code = geo_via_proxy(port)
    display = code if ok else "?"
    cache_set(f"geo:{name}", display=display, ok=ok)
    return display


# ── colors / draw ──────────────────────────────────────────────────────────

PAIR_TITLE = 1
PAIR_HEADER = 2
PAIR_HL = 3
PAIR_NORMAL = 4
PAIR_OK = 5
PAIR_ERR = 6
PAIR_DIM = 7
PAIR_UP = 8
PAIR_KEY = 9
PAIR_FOOTER = 10
PAIR_BORDER = 11
PAIR_BTN = 12
PAIR_ACCENT = 13


def init_colors():
    """Terminal default palette only — no solid bars, no custom RGB."""
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(PAIR_TITLE, curses.COLOR_CYAN, -1)
    curses.init_pair(PAIR_HEADER, curses.COLOR_CYAN, -1)
    curses.init_pair(PAIR_HL, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(PAIR_NORMAL, -1, -1)
    curses.init_pair(PAIR_OK, curses.COLOR_GREEN, -1)
    curses.init_pair(PAIR_ERR, curses.COLOR_RED, -1)
    curses.init_pair(PAIR_DIM, curses.COLOR_WHITE, -1)
    curses.init_pair(PAIR_UP, curses.COLOR_GREEN, -1)
    curses.init_pair(PAIR_KEY, curses.COLOR_YELLOW, -1)
    curses.init_pair(PAIR_FOOTER, curses.COLOR_WHITE, -1)
    curses.init_pair(PAIR_BORDER, curses.COLOR_WHITE, -1)
    curses.init_pair(PAIR_BTN, curses.COLOR_YELLOW, -1)
    curses.init_pair(PAIR_ACCENT, curses.COLOR_MAGENTA, -1)


def safe_addstr(win, y, x, text, attr=0):
    try:
        my, mx = win.getmaxyx()
        if 0 <= y < my and 0 <= x < mx:
            win.addstr(y, x, text[: mx - x - 1], attr)
    except curses.error:
        pass


def fill_rect(win, y, x, h, w, attr=0):
    for i in range(h):
        try:
            my, mx = win.getmaxyx()
            if y + i >= my:
                break
            win.addstr(y + i, x, " " * min(w, mx - x - 1), attr)
        except curses.error:
            pass


def draw_box(win, y, x, h, w, title="", active=False):
    """Outline box; active pane uses cyan border + purple title (no fill)."""
    if h < 2 or w < 4:
        return
    border = curses.color_pair(PAIR_HEADER if active else PAIR_BORDER)
    if active:
        border |= curses.A_BOLD
    try:
        win.addstr(y, x, "┌" + "─" * (w - 2) + "┐", border)
        for i in range(1, h - 1):
            win.addstr(y + i, x, "│", border)
            win.addstr(y + i, x + w - 1, "│", border)
        win.addstr(y + h - 1, x, "└" + "─" * (w - 2) + "┘", border)
        if title:
            label = f" {title} "
            tx = x + max(1, (w - len(label)) // 2)
            if active:
                title_attr = curses.color_pair(PAIR_HEADER) | curses.A_BOLD
            else:
                title_attr = curses.color_pair(PAIR_DIM)
            win.addstr(y, min(tx, x + w - len(label) - 1), label, title_attr)
    except curses.error:
        pass


def toast(stdscr, msg, error=False, ms=900):
    max_y, max_x = stdscr.getmaxyx()
    pair = PAIR_ERR if error else PAIR_OK
    safe_addstr(stdscr, max_y - 1, 0, " " * (max_x - 1))
    safe_addstr(
        stdscr, max_y - 1, 1, msg[: max_x - 3],
        curses.color_pair(pair) | curses.A_BOLD,
    )
    stdscr.refresh()
    curses.napms(ms)


def confirm(stdscr, prompt):
    max_y, max_x = stdscr.getmaxyx()
    w = min(max(len(prompt) + 10, 32), max_x - 4)
    h = 6
    bx = max(0, (max_x - w) // 2)
    by = max(0, (max_y - h) // 2)
    fill_rect(stdscr, by, bx, h, w)
    draw_box(stdscr, by, bx, h, w, "Confirm", active=True)
    safe_addstr(stdscr, by + 2, bx + 2, prompt[: w - 4], curses.color_pair(PAIR_NORMAL))
    safe_addstr(stdscr, by + 3, bx + 2, "<Yes>", curses.color_pair(PAIR_OK) | curses.A_BOLD)
    safe_addstr(stdscr, by + 3, bx + 10, "<No>", curses.color_pair(PAIR_ERR) | curses.A_BOLD)
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), 27, ord("q")):
            return False


# ── UI ─────────────────────────────────────────────────────────────────────

class DualPane:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.focus = "left"
        self.cfg_idx = 0
        self.act_idx = 0
        self.cfg_scroll = 0
        self._st_cache = {}
        self._en_cache = {}
        self._st_ttl = 2.0
        self.busy = ""

    def configs(self):
        return list_configs()

    def selected(self):
        cfgs = self.configs()
        if not cfgs:
            return None
        self.cfg_idx = max(0, min(self.cfg_idx, len(cfgs) - 1))
        return cfgs[self.cfg_idx]

    def status(self, name):
        now = time.time()
        hit = self._st_cache.get(name)
        if hit and now - hit[0] < self._st_ttl:
            return hit[1]
        st = service_status(name)
        self._st_cache[name] = (now, st)
        return st

    def enabled(self, name):
        now = time.time()
        hit = self._en_cache.get(name)
        if hit and now - hit[0] < self._st_ttl:
            return hit[1]
        en = service_enabled(name)
        self._en_cache[name] = (now, en)
        return en

    def inv_status(self, name=None):
        if name:
            self._st_cache.pop(name, None)
            self._en_cache.pop(name, None)
        else:
            self._st_cache.clear()
            self._en_cache.clear()

    def draw(self):
        scr = self.stdscr
        scr.erase()
        max_y, max_x = scr.getmaxyx()
        if max_y < 8 or max_x < 36:
            safe_addstr(scr, 0, 0, "too small")
            scr.refresh()
            return

        cfgs = self.configs()
        n_up = sum(1 for c in cfgs if self.status(c) == "up")
        safe_addstr(scr, 0, 1, "SingTUI", curses.color_pair(PAIR_ACCENT) | curses.A_BOLD)
        safe_addstr(scr, 0, 9, f" · {len(cfgs)} cfg · ", curses.color_pair(PAIR_DIM))
        safe_addstr(
            scr, 0, 9 + len(f" · {len(cfgs)} cfg · "),
            f"{n_up} up",
            curses.color_pair(PAIR_UP) | curses.A_BOLD if n_up else curses.color_pair(PAIR_DIM),
        )

        gap = 1
        right_w = max(14, min(22, int(max_x * 0.28)))
        left_w = max_x - right_w - gap
        top = 1
        pane_h = max_y - 2

        draw_box(
            scr, top, 0, pane_h, left_w, "Configs",
            active=(self.focus == "left"),
        )

        inner = pane_h - 2
        if self.cfg_idx < self.cfg_scroll:
            self.cfg_scroll = self.cfg_idx
        if self.cfg_idx >= self.cfg_scroll + inner:
            self.cfg_scroll = self.cfg_idx - inner + 1

        if not cfgs:
            safe_addstr(scr, top + 1, 2, "(empty — a/b)", curses.color_pair(PAIR_DIM))
        else:
            for i in range(inner):
                idx = self.cfg_scroll + i
                if idx >= len(cfgs):
                    break
                name = cfgs[idx]
                st = self.status(name)
                en = self.enabled(name)
                ping = cached_ping(name)
                geo = cached_geo(name)
                num = config_number(name) or (idx + 1)
                endpoint = outbound_endpoint(name)
                # "1. host:port  up  on  45ms  DE"
                head = f"{num}. {endpoint}"
                parts = [head, st, en]
                if ping:
                    parts.append(ping)
                if geo:
                    parts.append(geo)
                label = "  ".join(parts)
                y = top + 1 + i
                selected = self.focus == "left" and idx == self.cfg_idx
                if selected:
                    safe_addstr(
                        scr, y, 2, label[: left_w - 4].ljust(left_w - 4),
                        curses.color_pair(PAIR_HL) | curses.A_BOLD,
                    )
                else:
                    name_part = f"{head}  "
                    safe_addstr(scr, y, 2, name_part[: left_w - 4],
                                curses.color_pair(PAIR_NORMAL))
                    cx = 2 + len(name_part)
                    if cx >= left_w - 2:
                        continue
                    # active state
                    pair = PAIR_UP if st == "up" else (PAIR_ERR if st == "fail" else PAIR_DIM)
                    safe_addstr(
                        scr, y, cx, st,
                        curses.color_pair(pair) | (curses.A_BOLD if st == "up" else 0),
                    )
                    cx += len(st) + 2
                    if cx >= left_w - 2:
                        continue
                    # enabled state
                    en_pair = PAIR_KEY if en == "on" else PAIR_DIM
                    safe_addstr(
                        scr, y, cx, en,
                        curses.color_pair(en_pair) | (curses.A_BOLD if en == "on" else 0),
                    )
                    cx += len(en)
                    rest = ""
                    if ping:
                        rest += f"  {ping}"
                    if geo:
                        rest += f"  {geo}"
                    if rest and cx < left_w - 2:
                        safe_addstr(
                            scr, y, cx, rest[: left_w - cx - 2],
                            curses.color_pair(PAIR_NORMAL),
                        )

        rx = left_w + gap
        draw_box(
            scr, top, rx, pane_h, right_w, "Actions",
            active=(self.focus == "right"),
        )
        for i, (label, key, _) in enumerate(ACTIONS):
            y = top + 1 + i
            if y >= top + pane_h - 1:
                break
            text = f" {key}  {label}"
            if self.focus == "right" and i == self.act_idx:
                safe_addstr(
                    scr, y, rx + 1, text[: right_w - 3].ljust(right_w - 3),
                    curses.color_pair(PAIR_HL) | curses.A_BOLD,
                )
            else:
                safe_addstr(scr, y, rx + 2, key, curses.color_pair(PAIR_KEY) | curses.A_BOLD)
                safe_addstr(
                    scr, y, rx + 4, label[: right_w - 6], curses.color_pair(PAIR_NORMAL)
                )

        if self.busy:
            safe_addstr(
                scr, max_y - 1, 1, self.busy[: max_x - 3],
                curses.color_pair(PAIR_HEADER) | curses.A_BOLD,
            )
        else:
            # colored shortcut hints, no solid bar
            hints = [
                ("←→", PAIR_KEY), (" Tab ", PAIR_KEY),
                ("↑↓", PAIR_KEY), ("  ", PAIR_DIM),
                ("Enter", PAIR_KEY), ("  ", PAIR_DIM),
                ("a", PAIR_KEY), ("b", PAIR_KEY), ("e", PAIR_KEY),
                ("s", PAIR_KEY), ("x", PAIR_KEY), ("r", PAIR_KEY),
                ("n", PAIR_KEY), ("m", PAIR_KEY),
                ("p", PAIR_KEY), ("P", PAIR_KEY), ("g", PAIR_KEY),
                ("d", PAIR_KEY), ("q", PAIR_KEY),
            ]
            cx = 1
            for token, pair in hints:
                safe_addstr(scr, max_y - 1, cx, token, curses.color_pair(pair) | (
                    curses.A_BOLD if pair == PAIR_KEY else 0
                ))
                cx += len(token)

        scr.refresh()

    def run(self, aid):
        scr = self.stdscr
        cfg = self.selected()

        if aid == "quit":
            return "quit"

        if aid == "add":
            name = add_config()
            cfgs = self.configs()
            self.cfg_idx = cfgs.index(name) if name in cfgs else len(cfgs) - 1
            self.inv_status()
            toast(scr, f"+ {short_name(name)} :{port_for_config(name)}")
            return None

        if aid == "import":
            try:
                name = import_from_clipboard()
            except Exception as e:
                toast(scr, f"import: {e}", error=True, ms=1600)
                return None
            cfgs = self.configs()
            self.cfg_idx = cfgs.index(name) if name in cfgs else len(cfgs) - 1
            self.inv_status()
            toast(scr, f"imported {short_name(name)} :{port_for_config(name)}")
            return None

        if aid == "edit":
            if not cfg:
                toast(scr, "no config", error=True)
                return None
            editor = os.environ.get("EDITOR", "nano")
            curses.endwin()
            try:
                subprocess.call([editor, str(config_path(cfg))])
            except Exception:
                pass
            finally:
                scr.refresh()
                curses.doupdate()
            clear_config_cache(cfg)
            return None

        if aid in ("start", "stop", "restart", "enable", "disable"):
            if not cfg:
                toast(scr, "no config", error=True)
                return None
            ok, msg = service_action(cfg, aid)
            self.inv_status(cfg)
            if aid in ("stop", "restart", "disable"):
                clear_config_cache(cfg)
            toast(scr, msg, error=not ok)
            return None

        if aid == "ping":
            if not cfg:
                toast(scr, "no config", error=True)
                return None
            if self.status(cfg) != "up":
                toast(scr, "start proxy first", error=True)
                return None
            self.busy = f"ping {short_name(cfg)}…"
            self.draw()
            do_ping(cfg)
            self.busy = ""
            return None

        if aid == "ping_all":
            ups = [c for c in self.configs() if self.status(c) == "up"]
            if not ups:
                toast(scr, "no up proxies", error=True)
                return None
            for i, c in enumerate(ups):
                self.busy = f"ping {short_name(c)} ({i + 1}/{len(ups)})…"
                self.draw()
                do_ping(c)
            self.busy = ""
            toast(scr, f"pinged {len(ups)} up")
            return None

        if aid == "geo":
            if not cfg:
                toast(scr, "no config", error=True)
                return None
            if self.status(cfg) != "up":
                toast(scr, "start proxy first", error=True)
                return None
            self.busy = f"geo {short_name(cfg)}…"
            self.draw()
            do_geo(cfg)
            self.busy = ""
            return None

        if aid == "remove":
            if not cfg:
                toast(scr, "no config", error=True)
                return None
            if not confirm(scr, f"Delete {short_name(cfg)}?"):
                return None
            ok, msg = remove_config(cfg)
            self.inv_status()
            cfgs = self.configs()
            if self.cfg_idx >= len(cfgs):
                self.cfg_idx = max(0, len(cfgs) - 1)
            toast(scr, msg, error=not ok)
            return None

        return None

    def loop(self):
        curses.curs_set(0)
        keymap = {ord(k): a for _, k, a in ACTIONS}
        for _, k, a in ACTIONS:
            if k != "p":
                keymap[ord(k.upper())] = a

        while True:
            self.draw()
            ch = self.stdscr.getch()

            if ch == ord("P"):
                self.run("ping_all")
                continue

            if ch in keymap:
                if self.run(keymap[ch]) == "quit":
                    break
                continue

            if ch == curses.KEY_LEFT:
                self.focus = "left"
            elif ch == curses.KEY_RIGHT:
                self.focus = "right"
            elif ch == 9:
                self.focus = "right" if self.focus == "left" else "left"
            elif ch in (curses.KEY_UP, ord("k")):
                if self.focus == "left":
                    cfgs = self.configs()
                    if cfgs:
                        self.cfg_idx = (self.cfg_idx - 1) % len(cfgs)
                else:
                    self.act_idx = (self.act_idx - 1) % len(ACTIONS)
            elif ch in (curses.KEY_DOWN, ord("j")):
                if self.focus == "left":
                    cfgs = self.configs()
                    if cfgs:
                        self.cfg_idx = (self.cfg_idx + 1) % len(cfgs)
                else:
                    self.act_idx = (self.act_idx + 1) % len(ACTIONS)
            elif ch in (curses.KEY_ENTER, 10, 13):
                if self.focus == "left":
                    cfg = self.selected()
                    if cfg:
                        self.run("stop" if self.status(cfg) == "up" else "start")
                else:
                    if self.run(ACTIONS[self.act_idx][2]) == "quit":
                        break
            elif ch == 27:
                break


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    ensure_dirs()
    curses.wrapper(lambda s: (init_colors(), DualPane(s).loop()))


if __name__ == "__main__":
    main()
