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

from config import HTTP_PORTS, HTTPS_PORTS, Config, parse_preferred

# Module-level so it survives across requests served by the same isolate.
_override: list[str] | None = None


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
textarea { width: 100%; font: inherit; padding: .65rem .75rem; border-radius: 6px;
           border: 1px solid rgba(127,127,127,.35); background: rgba(127,127,127,.08);
           color: inherit; resize: vertical; min-height: 5.5rem; }
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


def _vless_uri(cfg: Config, host: str, address: str, port: str, tls: bool) -> str:
    params = {
        "encryption": "none",
        "security": "tls" if tls else "none",
        "type": "ws",
        "host": host,
        "path": cfg.ws_path,
    }
    if tls:
        params["sni"] = host
        params["fp"] = "randomized"
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    label = f"py-vless-{address}:{port}"
    return f"vless://{cfg.uuid}@{address}:{port}?{query}#{quote(label)}"


def build_links(
    cfg: Config, host: str, preferred: list[str] | None = None
) -> tuple[list[str], list[str]]:
    """Return (plaintext 80-series links, TLS 443-series links)."""
    addresses = preferred if preferred else effective_preferred(cfg)
    plain = [
        _vless_uri(cfg, host, address, port, tls=False)
        for address in addresses
        for port in HTTP_PORTS
    ]
    secure = [
        _vless_uri(cfg, host, address, port, tls=True)
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

    parts += [
        f"""<form method="post">
<textarea name="preferred" spellcheck="false" placeholder="每行一个域名,或用逗号分隔">{escape(chr(10).join(preferred))}</textarea>
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
