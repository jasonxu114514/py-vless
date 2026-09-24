"""Worker entry point: routes between the WebUI and the VLESS data plane."""

import base64
from urllib.parse import parse_qs, urlsplit

from workers import Response, WorkerEntrypoint

import config as config_mod
import page
from proxy import vless_fetch

_NOT_CONFIGURED = (
    "<!doctype html><meta charset=utf-8><title>py-vless</title>"
    '<div style="font:15px ui-monospace;max-width:44rem;margin:3rem auto;'
    "padding:1rem 1.25rem;border-left:3px solid #dc2626;"
    'background:rgba(220,38,38,.12);border-radius:4px">'
    "<b>py-vless is not configured.</b><br><br>{detail}"
    "<br><br>Set the <code>uuid</code> environment variable / secret:"
    "<br><code>npx wrangler secret put uuid</code></div>"
)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        upgrade = request.headers.get("Upgrade") or ""
        host = request.headers.get("Host") or ""

        try:
            cfg = config_mod.load(self.env)
        except config_mod.ConfigError as e:
            if upgrade.lower() == "websocket":
                return Response(f"configuration error: {e}", status=500)
            return Response(
                _NOT_CONFIGURED.replace("{detail}", str(e)),
                status=500,
                headers={"Content-Type": "text/html;charset=utf-8"},
            )

        if upgrade.lower() == "websocket":
            return await vless_fetch(self, request, cfg)

        url = urlsplit(request.url)
        path, method = url.path, request.method.upper()
        query = parse_qs(url.query)

        if path == f"/id/{cfg.uuid}" and method in ("GET", "POST"):
            return await self._config_page(request, cfg, host, query, method)

        if path == f"/id/{cfg.uuid}/sub":
            # ?preferred= previews a list for this request without persisting it.
            preview = (
                page.preview(query["preferred"][0]) if "preferred" in query else None
            )
            return self._subscription(cfg, host, query, preview)

        if path == "/":
            return Response("ok")

        return Response("not found", status=404)

    # -- WebUI ---------------------------------------------------------------

    async def _config_page(self, request, cfg, host, query, method):
        saved = False
        error = ""

        if method == "POST":
            fields = parse_qs(await request.text())
            if "reset" in fields:
                page.clear_preferred()
                saved = True
            else:
                submitted = (fields.get("preferred") or [""])[0]
                if page.set_preferred(submitted, cfg):
                    saved = True
                else:
                    error = "没有可用的域名 / no usable hostname in that list"
        # ?preferred= previews a list for this response only; saving needs a POST.
        seen = page.preview(query["preferred"][0]) if "preferred" in query else None

        return Response(
            page.render(cfg, host, saved=saved, error=error, preferred=seen),
            headers={"Content-Type": "text/html;charset=utf-8"},
        )

    # -- subscription --------------------------------------------------------

    def _subscription(self, cfg, host, query, preferred=None):
        plain, secure = page.build_links(cfg, host, preferred)
        tls = (query.get("tls") or [""])[0]
        if tls == "0":
            links = plain
        elif tls == "1":
            links = secure
        else:
            links = plain + secure

        return Response(
            base64.b64encode("\n".join(links).encode()).decode(),
            headers={
                "Content-Type": "text/plain;charset=utf-8",
                # Common subscription clients read these; harmless if ignored.
                "Profile-Update-Interval": "24",
            },
        )
