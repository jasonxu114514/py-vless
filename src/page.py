"""A small WebUI: shows the node links and lets the preferred-domain list be
edited at runtime.

Design notes:

* A submitted list is kept in the isolate's module-level state (`_override`) and
  applied to subsequent requests. It is **per-isolate, not global** — Cloudflare
  runs many isolates, so a save affects only whichever ones serve later requests.
  For a list that survives a redeploy and is identical everywhere, set the
  `preferred` environment variable instead. The page says so plainly.
* Editing requires the uuid as a path segment, so the page is no more exposed
  than the configuration it already prints.
* No CDN, no external references: everything is inline.
"""

from __future__ import annotations

import base64
from html import escape
from urllib.parse import quote

from config import (
    DEFAULT_DOH,
    DEFAULT_PROXYIP,
    ECH_PUBLIC_NAME,
    HTTP_PORTS,
    HTTPS_PORTS,
    Config,
    parse_ech,
    parse_preferred,
    parse_proxyip,
)

# Module-level so it survives across requests served by the same isolate.
_override: list[str] | None = None

# Sentinel distinguishing "not overridden" from "explicitly cleared".
_UNSET = object()
_proxyip_override = _UNSET
_ech_override = _UNSET


def effective_ech(cfg: Config):
    """The ECH settings in force: a per-isolate override, else the configured."""
    if _ech_override is _UNSET:
        return cfg.ech
    return parse_ech(_ech_override or None, cfg.ech.doh if cfg.ech else DEFAULT_DOH)


def set_ech(value: str, cfg: Config):
    """Apply a submitted ECH setting. Returns (accepted, effective)."""
    global _ech_override
    text = (value or "").strip()
    if not text:
        _ech_override = _UNSET
        return True, effective_ech(cfg)
    parsed = parse_ech(text, cfg.ech.doh if cfg.ech else DEFAULT_DOH)
    if parsed is None and text.lower() not in {
        "off", "none", "no", "n", "0", "false", "disable"
    }:
        return False, None
    _ech_override = text
    return True, parsed


def clear_ech() -> None:
    global _ech_override
    _ech_override = _UNSET


def effective_proxyip(cfg: Config):
    """The proxyIP in force: a per-isolate override, else the configured one."""
    if _proxyip_override is _UNSET:
        return cfg.proxyip
    return parse_proxyip(_proxyip_override or None)


# Spelled-out ways to turn the relay off, since a blank field means "default".
_DISABLE_WORDS = {"none", "off", "disable", "disabled", "0"}


def set_proxyip(value: str, cfg: Config):
    """Apply a submitted proxyIP. Returns (accepted, effective).

    A blank field means "go back to whatever is configured", so disabling the
    relay has to be spelled out. Invalid input is rejected rather than silently
    disabling, which would be a misconfiguration nobody notices.
    """
    global _proxyip_override
    text = (value or "").strip()
    if text.lower() in _DISABLE_WORDS:
        _proxyip_override = ""
        return True, None
    if not text:
        _proxyip_override = _UNSET
        return True, effective_proxyip(cfg)
    parsed = parse_proxyip(text)
    if parsed is None:
        return False, None
    _proxyip_override = text
    return True, parsed


def clear_proxyip() -> None:
    global _proxyip_override
    _proxyip_override = _UNSET


def effective_preferred(cfg: Config) -> list[str]:
    return _override if _override else cfg.preferred


def set_preferred(value: str, cfg: Config) -> list[str]:
    """Persist a submitted list. Returns what was actually accepted (may be [])."""
    global _override
    parsed = parse_preferred(value, [])
    _override = parsed or None
    return parsed


def preview(value: str) -> list[str]:
    """Parse a list for one response only, without persisting it."""
    return parse_preferred(value, [])


def clear_preferred() -> None:
    global _override
    _override = None


_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 15px/1.6 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
       max-width: 56rem; margin: 2.5rem auto; padding: 0 1.25rem; }
h1 { font-size: 1.3rem; margin: 0 0 .2rem; }
h2 { font-size: .82rem; margin: 2rem 0 .6rem; text-transform: uppercase;
     letter-spacing: .08em; opacity: .6; }
.sub { opacity: .6; margin: 0 0 1.5rem; }
.row { display: flex; gap: .6rem; align-items: center; margin-bottom: .5rem;
       background: rgba(127,127,127,.10); border-radius: 6px; padding: .5rem .7rem; }
.row .tag { min-width: 5.5rem; opacity: .6; flex-shrink: 0; }
.row code { flex: 1; overflow-wrap: anywhere; }
textarea, input[type=text] { width: 100%; font: inherit; padding: .65rem .75rem; border-radius: 6px;
           border: 1px solid rgba(127,127,127,.35); background: rgba(127,127,127,.08);
           color: inherit; }
textarea { resize: vertical; min-height: 5.5rem; margin-bottom: .6rem; }
input[type=text] { margin-bottom: .2rem; }
button { font: inherit; cursor: pointer; border: 0; border-radius: 5px;
         padding: .5rem .95rem; background: rgba(127,127,127,.25); }
button:hover { background: rgba(127,127,127,.42); }
button.ghost { background: transparent; border: 1px solid rgba(127,127,127,.4); }
.bar { display: flex; gap: .5rem; align-items: center; margin-top: .6rem;
       flex-wrap: wrap; }
.flash { border-left: 3px solid #16a34a; background: rgba(22,163,74,.12);
         padding: .6rem .85rem; border-radius: 4px; margin-bottom: 1rem; }
.note { opacity: .7; font-size: .88em; }
.ports code { margin-right: .35rem; }
"""


def _vless_uri(
    cfg: Config, host: str, address: str, port: str, tls: bool, ech=None
) -> str:
    params = {
        "encryption": "none",
        "security": "tls" if tls else "none",
        "type": "ws",
        "host": host,
        "path": cfg.ws_path,
    }
    if tls:
        params["sni"] = host
        params["fp"] = cfg.fp
        if ech is not None:
            # Encrypted Client Hello: the outer ClientHello carries `outer_name`
            # instead of `host`, so a blocked Worker hostname still connects.
            # Only meaningful with TLS, hence inside this branch.
            params["ech"] = ech.param
            if cfg.alpn:
                params["alpn"] = cfg.alpn
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    label = f"py-vless-{address}:{port}"
    return f"vless://{cfg.uuid}@{address}:{port}?{query}#{quote(label)}"


def build_links(
    cfg: Config, host: str, preferred: list[str] | None = None
) -> tuple[list[str], list[str]]:
    """Return (plaintext 80-series links, TLS 443-series links)."""
    addresses = preferred if preferred else effective_preferred(cfg)
    ech = effective_ech(cfg)
    plain = [
        _vless_uri(cfg, host, address, port, tls=False)
        for address in addresses
        for port in HTTP_PORTS
    ]
    secure = [
        _vless_uri(cfg, host, address, port, tls=True, ech=ech)
        for address in addresses
        for port in HTTPS_PORTS
    ]
    return plain, secure


def subscription(cfg: Config, host: str, preferred: list[str] | None = None) -> str:
    plain, secure = build_links(cfg, host, preferred)
    return base64.b64encode("\n".join(plain + secure).encode()).decode()


def _row(tag: str, value: str) -> str:
    return f'<div class="row"><span class="tag">{escape(tag)}</span><code>{escape(value)}</code></div>'


def render(
    cfg: Config,
    host: str,
    saved: bool = False,
    error: str = "",
    preferred: list[str] | None = None,
) -> str:
    preferred = preferred if preferred else effective_preferred(cfg)
    plain, secure = build_links(cfg, host, preferred)
    total = len(plain) + len(secure)

    parts: list[str] = [
        "<h1>py-vless</h1>",
        '<p class="sub">Cloudflare Workers 上的 VLESS 节点 · A VLESS proxy on Cloudflare Workers, written in Python.</p>',
    ]

    if saved:
        parts.append('<div class="flash">已保存。该修改对当前 isolate 生效,不跨 isolate、不跨重新部署。</div>')
    if error:
        parts.append(f'<div class="flash" style="border-color:#dc2626;background:rgba(220,38,38,.12)">{escape(error)}</div>')

    parts += [
        "<h2>节点信息 / Node</h2>",
        _row("uuid", cfg.uuid),
        _row("host", host),
        _row("path", cfg.ws_path),
        '<h2>优选域名 / Preferred domains</h2>',
    ]

    for address in preferred:
        parts.append(_row("preferred", address))

    active = effective_proxyip(cfg)
    is_builtin = active is not None and str(active) == DEFAULT_PROXYIP
    ech = effective_ech(cfg)
    ech_value = ech.param if ech else "off"
    q_ = f"({ech.doh})" if ech else ""
    parts += [
        "<h2>proxyIP</h2>",
        _row(
            "proxyIP",
            f"{active} (内置默认 / built-in default)" if is_builtin
            else (str(active) if active else "已关闭 / disabled"),
        ),
        _row("pyip override", f"https://{host}/id/{cfg.uuid}?pyip={active or '<host[:port]>'}"),
        "<h2>ECH (Encrypted Client Hello)</h2>",
        _row("ech", ech_value),
        _row("note", "TLS(443 系)节点有效 / applies to the TLS 443-series nodes"),
        f"""<input type="text" name="ech" spellcheck="false" value="{escape(ech_value)}" placeholder="off / auto / <域名> / <域名>+<DoH地址>">""",
        _row("ech override", f"https://{host}/id/{cfg.uuid}?ech={quote(ech_value, safe='')}"),
        f"""<form method="post">
<textarea name="preferred" spellcheck="false" placeholder="每行一个域名,或用逗号分隔">{escape(chr(10).join(preferred))}</textarea>
<input type="text" name="proxyip" spellcheck="false" value="{escape('' if is_builtin else (str(active) if active else 'none'))}" placeholder="留空=用内置默认;填 none 关闭;或填 host / host:port">
<div class="bar">
  <button type="submit">保存 / Save</button>
  <button type="submit" class="ghost" name="reset" value="1">恢复默认 / Reset</button>
  <span class="note">{len(preferred)} 个域名 · 生成 {total} 个节点</span>
</div>
</form>""",
        '<h2>单节点 / Single node</h2>',
        _row("ws + tls", secure[0]),
        _row("ws", plain[0]),
        "<h2>订阅 / Subscription</h2>",
        _row("all", f"https://{host}/id/{cfg.uuid}/sub"),
        _row("ws only", f"https://{host}/id/{cfg.uuid}/sub?tls=0"),
        _row("tls only", f"https://{host}/id/{cfg.uuid}/sub?tls=1"),
        "<h2>端口 / Ports</h2>",
        f'<p class="ports note">明文 / plaintext <code>{" ".join(HTTP_PORTS)}</code><br>'
        f'TLS <code>{" ".join(HTTPS_PORTS)}</code></p>',
        '<p class="note">协议 / protocol <code>vless</code> · 传输 / transport <code>ws</code> · '
        '加密 / encryption <code>none</code></p>',
        "<h2>说明 / Notes</h2>",
        '<p class="note">WebUI 的修改保存在 isolate 内存中,其他 isolate 与重新部署后都会恢复。'
        "要全局且持久,请设置 <code>preferred</code> 环境变量(逗号分隔)。<br>"
        "The WebUI edit is kept in isolate memory: it is not global and does not survive a "
        "redeploy. For a persistent, global list, set the <code>preferred</code> environment "
        "variable (comma-separated).</p>",
        f'<p class="note">当前默认使用公共 proxyIP <code>{DEFAULT_PROXYIP}</code>。输入框<b>留空</b>表示使用它,'
        "填 <code>none</code> 表示关闭,或填你自己的地址。<br>"
        f"The built-in default is the public relay <code>{DEFAULT_PROXYIP}</code>. Leave the field "
        "<b>blank</b> to use it, enter <code>none</code> to disable, or enter your own address.</p>",
        '<p class="note">proxyIP 是一台<strong>不在 Cloudflare 边缘</strong>的第三方中转服务器。'
        "Worker 不允许直连位于 Cloudflare 自己边缘上的源站,所以要访问 cloudflare.com、x.com、"
        "chatgpt.com 这类站点,必须经 proxyIP 绕行。它靠<strong>沿用 TLS SNI</strong> 工作:"
        "客户端的 ClientHello 里仍然写着真实目标,中转服务器据此转发。<br>"
        "因此:只有真正走 TLS 的流量能透过 proxyIP;proxyIP 能看到它所中转流量的目标;"
        "而且会消耗对方带宽 —— 这就是公共 proxyIP 经常失效的原因。建议自建。<br>"
        "A proxyIP is a third-party relay that is NOT behind Cloudflare. The Worker cannot reach "
        "origins on Cloudflare's own edge, so sites like cloudflare.com or x.com only work "
        "through one. It works by keeping the original TLS SNI, which the relay routes on. Only "
        "TLS traffic passes through it, it can see your destinations, and it costs its operator "
        "bandwidth — which is why public ones keep disappearing. Prefer your own.</p>",
        f'<p class="note"><b>ECH 的作用</b>:普通 TLS 会把真实 SNI(你的 Worker 域名)明文写在 '
        "ClientHello 里,一旦该域名被阻断,TLS 节点就全部不可用。ECH 让<b>外层</b> ClientHello 显示"
        f"另一个域名,而<b>内层</b>才是真实 SNI —— 所以只要外层域名不被阻断,你的节点就还能用。"
        f"目前配置为外层域名 <code>{ech.outer_name if ech else ''}</code>"
        f"{q_ or ''},通过 <code>{ech.doh if ech else ''}</code> 查询它的 HTTPS DNS 记录取得 ECH 公钥。"
        "**只对 TLS(443 系)节点有效**,80 系明文节点是明文,没有 SNI 可保护。<br>"
        "写法:<code>off</code> 关闭;<code>auto</code> 用内置的 "
        f"<code>{ECH_PUBLIC_NAME}</code>;<code>&lt;域名&gt;</code> 只换外层域名;"
        "<code>&lt;域名&gt;+&lt;DoH地址&gt;</code> 两者都自定义(也可用 <code>|</code> 分隔,更好输入)。<br>"
        "注意 <code>alpn</code> 必须是 <code>http/1.1</code>:WebSocket 传输走的是 HTTP/1.1 Upgrade,"
        "若服务端选中 h2,握手会失败并报 <code>websocket: protocol \"h2\" is not supported</code>。"
        "实测把 <code>h2</code> 放进列表会让所有节点失效。<br>"
        "注意:<b>ECH 值不合法时必须留空而不是写错</b> —— Xray 在解析失败时会故意使用一个无效配置"
        "让连接直接失败,而不是降级为无 ECH。<br>"
        "<b>What ECH buys you:</b> normally the real SNI (your Worker hostname) travels in cleartext in "
        "the ClientHello, so blocking that name kills every TLS node. ECH puts a different name in the "
        "<i>outer</i> ClientHello and the real one inside, so the nodes keep working as long as the "
        "outer name is not blocked. TLS (443-series) nodes only — the plaintext 80-series nodes have no "
        "SNI to protect. <code>off</code> disables, <code>auto</code> uses the built-in "
        f"<code>{ECH_PUBLIC_NAME}</code>, <code>&lt;name&gt;</code> changes just the outer name, and "
        "<code>&lt;name&gt;+&lt;DoH&gt;</code> sets both. A malformed value makes the connection fail "
        "rather than silently downgrade, so leave it empty instead of guessing.</p>",
        '<p class="note">Cloudflare 前置的站点无法通过本代理访问(cloudflare.com、x.com、'
        "chatgpt.com 等):Worker 无法连接与自身同源边缘的站点。原版 JS 用 proxyIP 绕过,"
        "本版本未实现。<br>"
        "Cloudflare-fronted destinations cannot be proxied, because the Worker cannot connect "
        "to an origin sharing Cloudflare's own edge. The JS original works around this with a "
        "proxyIP; this build does not implement one.</p>",
    ]

    return (
        "<!doctype html><html lang=zh><head><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>py-vless</title><style>{_STYLE}</style></head><body>"
        + "\n".join(parts)
        + "</body></html>"
    )
