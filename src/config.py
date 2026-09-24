"""Environment-variable configuration.

Unlike the JavaScript original, there is deliberately **no default uuid**. The
original ships a well-known public one (`86c50e3a-...`), which means an
unconfigured deployment is world-usable. Here a missing or malformed uuid is a
hard error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    uuid: str
    path: str | None
    preferred: list[str]
    ws_path: str

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

    return Config(
        uuid=uuid,
        path=_get(env, "path"),
        preferred=parse_preferred(preferred_raw, DEFAULT_PREFERRED),
        ws_path=_get(env, "ws_path") or DEFAULT_WS_PATH,
    )
