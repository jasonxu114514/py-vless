# py-vless

[中文文档](README.zh-CN.md)

A VLESS node for Cloudflare Workers, written in Python.

**What you get**

- A VLESS node running on Cloudflare's edge — no server of your own.

## Deploy

1. **Fork this repository** on GitHub.
2. Open the deploy page:

   ```
   https://deploy.workers.cloudflare.com/?url=https://github.com/jasonxu114514/py-vless
   ```

3. Sign in to Cloudflare when prompted and pick the account to deploy into.
4. Fill in the fields the form asks for:

   | Field | What to put there |
   | :--- | :--- |
   | `uuid` | Your VLESS user id — a UUIDv4. Generate one with `python -c "import uuid; print(uuid.uuid4())"` or any UUID generator. **Required.** |
   | `preferred` | Preferred domains, comma-separated. Leave the default to start. |
   | `proxyip` | Relay for reaching Cloudflare-fronted sites. Prefilled with a public address; clear it to turn the fallback off. |

5. Click **Deploy**. When it finishes, Cloudflare shows your Worker URL, for example
   `https://py-vless.<your-subdomain>.workers.dev`.
6. Open your WebUI at:

   ```
   https://py-vless.<your-subdomain>.workers.dev/id/<your-uuid>
   ```

> To have later pushes redeploy automatically, connect the repo in the Cloudflare dashboard via
> **Workers & Pages** → **Create application** → **Workers** → **Connect to Git**, and pick your
> fork. Build settings: build command `uvx --from workers-py pywrangler sync`, deploy command
> `npx wrangler deploy`, root directory empty. The Worker you create must be named exactly
> `py-vless` to match `name` in `wrangler.jsonc`, or the build fails; and the deploy command
> must be `npx wrangler deploy` rather than `pywrangler deploy`, which would run the sync twice.

### After deploying

The very first request may fail with a TLS handshake error while the isolate warms up. Retry
once and it clears.

## Configuration

Configure through environment variables. In the Cloudflare dashboard these live under
**your Worker → Settings → Variables and Secrets**.

| Variable | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `uuid` | **yes** | *(none)* | Your VLESS user id, a UUIDv4. The Worker returns a configuration error until this is set. |
| `preferred` | no | `www.shopify.com`,`mfa.gov.ua`,`www.visa.cn`,`store.ubi.com` | Comma-separated hostnames used as the **server address** in your node links. Any hostname that resolves onto Cloudflare's edge works, so you can swap in your own or a domain you already own. Up to 30. |
| `path` | no | *(any path)* | If set, WebSocket upgrades are accepted only on this path. Leave unset unless you want to restrict it. |
| `ws_path` | no | `/?ed=2560` | The path published inside the generated share links. `ed=2560` is the 0-RTT early-data hint that common clients use. Change it only if your client needs something different. |
| `ech` | no | `cloudflare-ech.com+https://cloudflare-dns.com/dns-query` | **Encrypted Client Hello.** If your Worker hostname is SNI-blocked, every TLS node dies — ECH makes the *outer* ClientHello show another name while the real SNI is encrypted inside. Only the TLS (443-series) nodes carry it. Forms: `off`; `auto`; `<name>`; `<name>+<DoH>` (or `|` instead of `+`). |
| `doh` | no | `https://cloudflare-dns.com/dns-query` | DoH server used to look up the ECH config (the name's DNS HTTPS record). |
| `alpn` | no | `http/1.1` | **Must be `http/1.1`.** The WebSocket transport performs an HTTP/1.1 Upgrade; if the server picks h2 you get `websocket: protocol "h2" is not supported`. Putting h2 in this list breaks every node. Blank omits the parameter. |
| `fp` | no | `chrome` | TLS fingerprint. **`randomized` cannot be used with ECH**: it picks a random curve and Go then fails to build the outer ClientHello (`tls: malformed outer client hello`). The original scripts use randomized; this is a deliberate change. |
| `proxyip` | no | `proxyip.cmliussss.net` | Relay server, as `host`, `host:port`, `[IPv6]` or `[IPv6]:port` (default port 443). Needed to reach **Cloudflare-fronted sites** (cloudflare.com, x.com, chatgpt.com): the Worker is not allowed to connect to origins on Cloudflare's own edge, so those must be routed around. Examples: `1.2.3.4`, `1.2.3.4:8443`, `proxy.example.com`, `[2001:db8::1]:8443`. Alias: `pyip`. **An empty value disables it**; so does a malformed one, rather than falling back to the default. |

### About ECH

If your Worker hostname (`*.workers.dev`) is SNI-blocked, **every TLS node stops working** —
ordinary TLS puts the real name in cleartext in the ClientHello, where a blocking device can
read it.

ECH inverts that: the *outer* ClientHello shows a different name, and the real SNI is encrypted
inside. As long as the outer name is not blocked, the nodes keep working. The default outer name
is `cloudflare-ech.com` (Cloudflare publishes its ECH key in that name's HTTPS DNS record),
looked up over Cloudflare's DoH.

Measured with xray-core 26.3.27 against a live Worker:

| Configuration | Result |
| :--- | :--- |
| `fp=chrome` + `alpn=http/1.1` + ECH | ✅ everything works, including Cloudflare-fronted sites |
| `fp=randomized` + ECH | ❌ `tls: malformed outer client hello` — every node fails |
| `alpn=h2,http/1.1` + `type=ws` | ❌ `websocket: protocol "h2" is not supported` |

That is why the generated links use `fp=chrome` with `alpn=http/1.1`.

> A malformed ECH value makes Xray substitute a deliberately invalid config so the connection
> fails, rather than downgrading to no ECH — so a typo here takes you fully offline. Prefer
> leaving it empty over guessing.

### About proxyIP

Cloudflare does not let a Worker connect to an origin sitting on **Cloudflare's own edge**, so
`cloudflare.com`, `x.com`, `chatgpt.com` and similar fail outright without one. A proxyIP is a
third-party server that is **not** behind Cloudflare, and the connection is re-made through it.

It works because the client's TLS ClientHello already names the **real destination** — that is
what the VLESS address is — so the relay reads the SNI and knows where to forward. The bytes are
passed through **completely unchanged**.

Three consequences worth understanding:

- **Only traffic that actually speaks TLS gets through.** Plaintext has no SNI, so the relay
  cannot tell where to send it.
- **The proxyIP operator can see the destinations you visit.** Do not send anything sensitive
  through somebody else's server.
- **It costs them bandwidth**, which is why public proxyIPs keep disappearing. Prefer your own:
  any small VPS that is *not* behind Cloudflare, set up with something like
  [x-ui-yg](https://github.com/yonggekkk/x-ui-yg).

The default is the public relay `proxyip.cmliussss.net`. **Setting it empty disables the
fallback**, leaving direct connections only. A malformed value also disables it, deliberately:
better a dead relay than one typo silently routing your traffic through a server you did not
mean to use.

You can also override it per connection, in either of these forms:

```
/id/<uuid>?pyip=1.2.3.4            # from the WebUI, or in the address bar
wss://…/?ed=2560&pyip=1.2.3.4      # in the client's WebSocket path
/<uuid> or any path containing /pyip=1.2.3.4   # the original JS form, fully compatible
```

Like `preferred`, the WebUI's proxyIP field is **per-isolate and does not survive a redeploy**.
Use the environment variable for something lasting. In the field, **blank means "use the
default relay"**, `none` turns it off, and anything else is taken as an address.

## Using it

### Node links

Open `/id/<uuid>` for a single node link in both flavours:

- **ws** — plaintext, for the 80-series ports (80, 8080, 8880, 2052, 2082, 2086, 2095)
- **ws + tls** — TLS, for the 443-series ports (443, 2053, 2083, 2087, 2096, 8443)

Copy a link into your client, or scan it as a QR code.

> **The port must match the security mode.** The 80-series ports are plaintext
> (`security=none`); the 443-series ports use TLS (`security=tls`). A client set up for one will
> not work against the other.

### Subscription

| URL | Contents |
| :--- | :--- |
| `/id/<uuid>/sub` | Everything: every preferred domain × 13 ports |
| `/id/<uuid>/sub?tls=0` | Only the 7 plaintext ports |
| `/id/<uuid>/sub?tls=1` | Only the 6 TLS ports |

The body is a base64-encoded list of `vless://` links. Paste the URL into your client's
"add subscription" field. With the four default domains this is 52 nodes.

### Recommended client settings

| Setting | Value |
| :--- | :--- |
| Protocol | `vless` |
| Transport | `ws` |
| Encryption | `none` |
| Address | a preferred domain |
| Port | 443 (with TLS) or 80 (without), or any of the 13 |
| Host / SNI | your Worker's hostname |
| Path | `/?ed=2560` |

Enabling **fragmentation** in your client helps when the Worker's own hostname is blocked, since
it breaks up the TLS handshake.

## Credits

Thanks to the [Linux.do](https://linux.do) community.

The protocol handling follows the community lineage the original scripts credit:
[ca110us/epeius](https://github.com/ca110us/epeius),
[3Kmfi6HP/EDtunnel](https://github.com/3Kmfi6HP/EDtunnel) and the
[zizifn/excalidraw-backup](https://github.com/zizifn/excalidraw-backup) VLESS reference.
