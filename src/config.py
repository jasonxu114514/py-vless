"""Environment-variable configuration.

Unlike the JavaScript original, there is deliberately **no default uuid**. The
original ships a well-known public one (`86c50e3a-...`), which means an
unconfigured deployment is world-usable. Here a missing or malformed uuid is a
hard error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from vless import is_valid_uuid

# Preferred (CDN) domains used to build the share links and the subscription.
# These are ordinary hostnames that resolve onto Cloudflare's edge, which makes
# them usable as the *server* address in a client. Any hostname works; these
# four are the defaults.
DEFAULT_PREFERRED = [
    "www.shopify.com",
    "mfa.gov.ua",
    "www.visa.cn",
    "store.ubi.com",
]

# The 13 standard Cloudflare ports. The first seven are plaintext (no TLS), the
# remaining six are TLS. A client must match the port to the security mode.
HTTP_PORTS = ["80", "8080", "8880", "2052", "2082", "2086", "2095"]
HTTPS_PORTS = ["443", "2053", "2083", "2087", "2096", "8443"]

# The path published in share links. `ed=2560` is the 0-RTT early-data hint used
# by the original script and by common clients.
DEFAULT_WS_PATH = "/?ed=2560"

MAX_PREFERRED = 30
MAX_HOST_LEN = 253

# Placeholders that a deploy template may leave behind. Treated as "unset",
# because Cloudflare's one-click flow only surfaces variables declared in
# wrangler.jsonc -- but an empty var shadows a secret of the same name.
PLACEHOLDERS = {
    "replace_with_your_uuid",
    "your_uuid",
    "your-uuid",
    "changeme",
    "change_me",
    "todo",
}

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_IPV6_RE = re.compile(r"^[0-9a-f:]+$")

# Default relay for reaching Cloudflare-fronted sites. This is a third-party
# public service, not ours: traffic to those sites passes through it, so it can
# see your destinations and it costs its operator bandwidth (which is why public
# relay addresses change over time). Set `proxyip` to your own, or to an empty
# string to disable the fallback entirely.
DEFAULT_PROXYIP = "proxyip.cmliussss.net"

# Encrypted Client Hello. The point of ECH is that the *outer* ClientHello shows
# a name other than your own, so a Worker hostname that is blocked can still be
# reached. The outer name must publish an ECH config in its DNS HTTPS record;
# Cloudflare publishes one for cloudflare-ech.com (and for *.workers.dev too).
#
# The value is emitted in the form Xray understands for a DNS lookup:
#     <name-to-query>+<DoH endpoint>
# Xray queries the HTTPS (type 65) record for that name at that DoH server and
# extracts the `ech` parameter. A malformed value makes Xray substitute a
# deliberately invalid config so the connection FAILS, rather than silently
# downgrading to an unencrypted handshake -- so it is better to omit ECH than to
# emit something wrong.
ECH_PUBLIC_NAME = "cloudflare-ech.com"
DEFAULT_DOH = "https://cloudflare-dns.com/dns-query"

# TLS client fingerprint for the generated links.
#
# MUST NOT be "randomized" when ECH is enabled. Xray's randomized fingerprint
# picks a random curve/set per connection, and Go's TLS then fails to build the
# ECH outer ClientHello:
#     tls: malformed outer client hello
# (and, without ECH, sometimes `tls: CurvePreferences includes unsupported
# curve`). Verified against a live Worker: chrome + ECH => 200,
# randomized + ECH => every node fails. The original scripts use randomized, so
# this is a deliberate departure.
DEFAULT_FP = "chrome"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Ech:
    """Resolved ECH settings for the generated links."""

    #: name whose HTTPS record carries the ECH config
    outer_name: str
    #: DoH endpoint Xray should query it from
    doh: str

    @property
    def param(self) -> str:
        """The `ech=` value in the form Xray expects for a DNS lookup."""
        return f"{self.outer_name}+{self.doh}"


@dataclass(frozen=True)
class ProxyIP:
    """A fallback relay used when a direct connection yields nothing."""

    host: str
    port: int = 443

    def __str__(self) -> str:
        return self.host if self.port == 443 else f"{self.host}:{self.port}"


@dataclass(frozen=True)
class Config:
    uuid: str
    path: str | None
    preferred: list[str]
    ws_path: str
    proxyip: ProxyIP | None = None
    ech: Ech | None = None
    #: ALPN advertised in the links, or "" to omit the parameter.
    #: Must be http/1.1 for `type=ws`: Xray's WebSocket transport performs an
    #: HTTP/1.1 Upgrade, and if the server selects h2 the dial fails with
    #: `websocket: protocol "h2" was given but is not supported`. Verified
    #: against a live Worker -- putting h2 in this list breaks every node.
    alpn: str = "http/1.1"
    #: TLS client fingerprint advertised in the links.
    fp: str = DEFAULT_FP

    @property
    def proxyip_is_default(self) -> bool:
        return self.proxyip is not None and str(self.proxyip) == DEFAULT_PROXYIP

    @property
    def path_is_enforced(self) -> bool:
        return bool(self.path)


def _get(env, name: str) -> str | None:
    try:
        value = getattr(env, name)
    except AttributeError:
        return None
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def parse_preferred(raw: str | None, fallback: list[str]) -> list[str]:
    """Split and validate a comma/whitespace separated hostname list.

    Invalid entries are dropped rather than raising: the list arrives straight
    from a web form or an environment variable, and one typo should not take the
    whole page down. Order is preserved and duplicates removed.
    """
    if not raw:
        return list(fallback)
    out: list[str] = []
    for item in re.split(r"[,\s]+", raw.strip()):
        host = item.strip().lower()
        if not host or host in out:
            continue
        if len(host) > MAX_HOST_LEN or not _HOST_RE.match(host):
            continue
        out.append(host)
        if len(out) >= MAX_PREFERRED:
            break
    return out or list(fallback)


def _get_raw(env, name: str) -> str | None:
    """Like _get, but keeps a deliberately-blank value as "" rather than None."""
    try:
        value = getattr(env, name)
    except AttributeError:
        return None
    if value is None:
        return None
    return str(value).strip()


def parse_proxyip(raw: str | None) -> ProxyIP | None:
    """Parse `host`, `host:port`, `[v6]` or `[v6]:port`. None if unusable."""
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None

    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        host = value[1:end]
        rest = value[end + 1 :]
        if rest and not rest.startswith(":"):
            return None
        port = 443
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
    elif value.count(":") == 1:
        host, _, port_s = value.partition(":")
        port = int(port_s) if port_s.isdigit() else 443
    else:
        # A bare hostname, a bare IPv4, or an unbracketed IPv6 literal.
        host, port = value, 443

    host = host.strip().lower()
    if not 0 < port < 65536:
        return None
    if not (_HOST_RE.match(host) or _IPV4_RE.match(host) or _IPV6_RE.match(host)):
        return None
    return ProxyIP(host=host, port=port)


def parse_ech(raw: str | None, doh_default: str = DEFAULT_DOH) -> Ech | None:
    """Parse the `ech` setting.

    Accepts, case-insensitively:
      * off / none / no / 0 / ""   -> ECH disabled
      * on / auto / yes / y        -> the built-in public name
      * <name>                     -> that name, at the default DoH
      * <name>+<doh> / <name>|<doh>-> that name at that DoH endpoint
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text or text.lower() in {"off", "none", "no", "n", "0", "false", "disable"}:
        return None
    if text.lower() in {"on", "auto", "yes", "y", "1", "true", "enable"}:
        return Ech(ECH_PUBLIC_NAME, doh_default)

    for sep in ("+", "|"):
        if sep in text:
            name, _, doh = text.partition(sep)
            name, doh = name.strip().lower(), doh.strip()
            if not name or not doh.startswith("https://"):
                return None
            if not _HOST_RE.match(name):
                return None
            return Ech(name, doh)

    name = text.lower()
    if not _HOST_RE.match(name):
        return None
    return Ech(name, doh_default)


def resolve_proxyip(cfg: Config, url: str) -> ProxyIP | None:
    """proxyIP for one connection: a per-request override, else the configured one.

    Two override spellings are accepted, both matching what the original scripts
    and their generated links use:
      * `/pyip=<host>` in the path (the original's own form, so existing client
        configurations keep working);
      * `?pyip=<host>` in the query, which is easier to set by hand.
    An unusable override is ignored rather than fatal, falling back to the
    configured value.
    """
    parts = urlsplit(url)
    override = None
    if "/pyip=" in parts.path:
        override = parts.path.split("/pyip=", 1)[1].split("/")[0]
    elif parts.query:
        for item in parts.query.split("&"):
            if item.startswith("pyip="):
                override = item[5:]
                break
    if override:
        parsed = parse_proxyip(override)
        if parsed is not None:
            return parsed
    return cfg.proxyip


def load(env) -> Config:
    """Build the config from the Worker's environment bindings."""
    raw_uuid = _get(env, "uuid")
    if not raw_uuid:
        raise ConfigError(
            "the `uuid` environment variable is required and has no default. "
            "Set it in the Cloudflare dashboard under your Worker > Settings > "
            "Variables and Secrets, or as a secret with "
            "`npx wrangler secret put uuid`."
        )
    # A client may be given several comma-separated uuids, as in the original.
    uuid = raw_uuid.split(",")[0].strip().lower()
    if uuid in PLACEHOLDERS:
        raise ConfigError(
            "`uuid` is still the placeholder from the deploy template. Replace it "
            "with your own UUIDv4 (the default template value is not a valid uuid "
            "and must not be used)."
        )
    if not is_valid_uuid(uuid):
        raise ConfigError(
            f"`uuid` must be a UUIDv4 such as 86c50e3a-5b87-49dd-bd20-03c7f2735e40, got {uuid!r}"
        )

    preferred_raw = _get(env, "preferred")

    # `pyip` is the shorter spelling used in the original scripts. Read raw so
    # that an explicit empty value means "disabled" and an absent one falls back
    # to the default relay.
    raw = _get_raw(env, "proxyip")
    if raw is None:
        raw = _get_raw(env, "pyip")
    if raw is None:
        proxyip = parse_proxyip(DEFAULT_PROXYIP)
    else:
        proxyip = parse_proxyip(raw)  # "" disables; junk falls back to None too

    doh = _get(env, "doh") or DEFAULT_DOH
    ech = parse_ech(_get_raw(env, "ech"), doh)

    return Config(
        uuid=uuid,
        path=_get(env, "path"),
        preferred=parse_preferred(preferred_raw, DEFAULT_PREFERRED),
        ws_path=_get(env, "ws_path") or DEFAULT_WS_PATH,
        proxyip=proxyip,
        ech=ech,
        alpn=_get(env, "alpn") or "http/1.1",
        fp=_get(env, "fp") or DEFAULT_FP,
    )
