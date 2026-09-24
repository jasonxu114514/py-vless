"""VLESS protocol: header parsing and response construction.

Pure Python, no JS interop -- so this is fully unit-testable locally.

Wire layout of a VLESS request (N = addons length, the byte at offset 17):

    offset  size  field
    0       1     version (echoed back in the response header)
    1       16    UUID raw bytes
    17      1     N, length of the addons (MUX params)
    18      N     addons (skipped, not parsed)
    18+N    1     command: 1=TCP, 2=UDP, 3=MUX
    19+N    2     port, big-endian uint16
    21+N    1     address type: 1=IPv4, 2=domain, 3=IPv6
    22+N    var   address
    rawDataIndex  first payload (usually the TLS ClientHello; often empty)

`rawDataIndex` is 26+N for IPv4, 23+N+L for a domain (L = the length byte), and
38+N for IPv6.

Deviations from the JavaScript original, both deliberate:

  * It assumes the whole header arrives in the first WebSocket frame. We
    accumulate across frames instead, because a client may split the header.
  * It throws on a UUID whose version/variant nibbles are not UUIDv4, so such a
    client gets a hard stream error instead of a clean rejection. We just do a
    straight string compare.
"""

from __future__ import annotations

import base64
import ipaddress
import re
from dataclasses import dataclass

# --- constants -------------------------------------------------------------

CMD_TCP = 1
CMD_UDP = 2
CMD_MUX = 3

ADDR_IPV4 = 1
ADDR_DOMAIN = 2
ADDR_IPV6 = 3

# A VLESS response header is exactly two bytes: the request's version byte,
# followed by a zero addons length.
RESPONSE_HEADER_TAIL = b"\x00"

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)

_BYTE_TO_HEX = [(i + 256).__format__("x")[1:] for i in range(256)]


class VlessError(Exception):
    """Unrecoverable problem with a VLESS request."""


class HeaderIncomplete(Exception):
    """The buffer does not yet hold a complete header; read more and retry."""


@dataclass
class Header:
    version: int
    uuid: str
    command: int
    host: str
    port: int
    raw_data_index: int

    @property
    def is_udp(self) -> bool:
        return self.command == CMD_UDP

    @property
    def response_header(self) -> bytes:
        return bytes([self.version]) + RESPONSE_HEADER_TAIL


def unsafe_stringify(arr, offset: int = 0) -> str:
    """16 bytes -> canonical UUID text, big-endian, no byte swapping.

    Mirrors the JS `unsafeStringify` helper; it never raises.
    """
    h = _BYTE_TO_HEX
    return (
        h[arr[offset + 0]]
        + h[arr[offset + 1]]
        + h[arr[offset + 2]]
        + h[arr[offset + 3]]
        + "-"
        + h[arr[offset + 4]]
        + h[arr[offset + 5]]
        + "-"
        + h[arr[offset + 6]]
        + h[arr[offset + 7]]
        + "-"
        + h[arr[offset + 8]]
        + h[arr[offset + 9]]
        + "-"
        + h[arr[offset + 10]]
        + h[arr[offset + 11]]
        + h[arr[offset + 12]]
        + h[arr[offset + 13]]
        + h[arr[offset + 14]]
        + h[arr[offset + 15]]
    )


def is_valid_uuid(value: str) -> bool:
    """UUIDv4-shaped check, used to validate the configured uuid at startup."""
    return bool(_UUID_RE.fullmatch(value or ""))


def parse_header(buf: bytes) -> Header:
    """Parse a complete VLESS header, or raise HeaderIncomplete if short."""
    if len(buf) < 18:
        raise HeaderIncomplete

    version = buf[0]
    uuid = unsafe_stringify(buf, 1)
    addons_len = buf[17]

    cmd_index = 18 + addons_len
    if len(buf) < cmd_index + 1:
        raise HeaderIncomplete
    command = buf[cmd_index]

    port_index = cmd_index + 1
    if len(buf) < port_index + 2:
        raise HeaderIncomplete
    port = int.from_bytes(buf[port_index : port_index + 2], "big")

    addr_type_index = port_index + 2
    if len(buf) < addr_type_index + 1:
        raise HeaderIncomplete
    addr_type = buf[addr_type_index]

    value_index = addr_type_index + 1

    if addr_type == ADDR_IPV4:
        if len(buf) < value_index + 4:
            raise HeaderIncomplete
        host = ".".join(str(b) for b in buf[value_index : value_index + 4])
        raw_data_index = value_index + 4
    elif addr_type == ADDR_DOMAIN:
        if len(buf) < value_index + 1:
            raise HeaderIncomplete
        length = buf[value_index]
        if len(buf) < value_index + 1 + length:
            raise HeaderIncomplete
        host = buf[value_index + 1 : value_index + 1 + length].decode("utf-8", "replace")
        raw_data_index = value_index + 1 + length
    elif addr_type == ADDR_IPV6:
        if len(buf) < value_index + 16:
            raise HeaderIncomplete
        host = str(ipaddress.IPv6Address(bytes(buf[value_index : value_index + 16])))
        raw_data_index = value_index + 16
    else:
        raise VlessError(f"unsupported address type {addr_type}")

    if not host:
        raise VlessError(f"empty address for address type {addr_type}")

    return Header(
        version=version,
        uuid=uuid,
        command=command,
        host=host,
        port=port,
        raw_data_index=raw_data_index,
    )


def base64_to_bytes(value: str) -> bytes | None:
    """Decode 0-RTT early data from the Sec-WebSocket-Protocol header.

    An empty header is a no-op (returns None), not an error. HTML
    "forgiving-base64" semantics: URL-safe alphabet, missing padding tolerated,
    a remainder of 1 is invalid.
    """
    if not value:
        return None
    text = value.replace("-", "+").replace("_", "/")
    if len(text) % 4 == 1:
        raise VlessError("invalid base64 length in early data")
    text += "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text, validate=False)
    except Exception as e:  # noqa: BLE001
        raise VlessError(f"invalid base64 in early data: {e}") from e
