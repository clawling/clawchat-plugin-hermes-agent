# Media boundaries

Inbound transport and outbound path rules; see [turn authority](non-owner-boundaries.md) for caller checks.

## Inbound media downloads

Inbound downloads allow HTTPS only; `media_allow_http` explicitly enables HTTP.
Relative URLs use the media base derived from the [Media settings](configuration.md#media) and undergo the
same checks. Non-HTTP(S) schemes are rejected before joining a URL to its base.
For a direct connection, each hop resolves the hostname once, rejects the
request if **any** resolved address is not public unicast, and connects to a validated IP while retaining
the hostname for Host and TLS verification. Localhost names are also rejected.
Connection failures try the remaining distinct validated IPs in order without
resolving again. TCP connection and TLS handshake each use a timeout capped by
the remaining attachment deadline. At most four inbound DNS lookups may remain
pending; further lookups are rejected until a slot becomes available.
Every redirect chooses its route again and repeats the applicable checks, with
at most five redirects; HTTPS-to-HTTP redirects require the HTTP opt-in too.
Backslashes in redirect destinations are
normalized before resolving and validating their target host.

`media_proxy_mode` controls environment proxies on every hop:

- `auto` (default): use the target scheme's proxy from Python's
  `urllib.request.getproxies()` (including `HTTP_PROXY` / `HTTPS_PROXY`). Without
  a proxy for that scheme, connect directly with full address validation.
  Unsupported proxy URLs, including `socks5://`, fall back to direct connections
  with a warning once per process; the warning omits the proxy URL.
- `on`: require a supported proxy for that scheme. A missing or unsupported
  proxy rejects the download.
- `off`: always connect directly with full DNS / public-address checks and IP
  pinning, ignoring environment proxies.

In `auto` and `on`, a hostname matched by `urllib.request.proxy_bypass(host)`
(`no_proxy` / `NO_PROXY`) takes the direct path with full address validation.
This also applies after redirects. Only `http://` proxy URLs are supported:
HTTPS targets use CONNECT followed by TLS with target hostname verification;
HTTP targets use an absolute-URI GET and still require `media_allow_http`.
The proxy itself may have a private address.

**Residual risk:** when `auto` (default) or `on` uses a proxy, target hostname
DNS resolution and private-network checks are delegated to that proxy. The
plugin rejects private, loopback and link-local IP literals before contacting
the proxy, and rejects localhost names, unless `media_allow_private_network`
is enabled. It does not resolve other target hostnames locally. If the proxy
can access private networks, the plugin cannot prevent a request whose
hostname resolves to an internal address. Set `media_proxy_mode: "off"` for
strict local destination validation (and leave `media_allow_private_network`
disabled).

Downloads **never attach Authorization** to the target, even for URLs on the
configured base origin: media URLs must be public or presigned GET URLs.
Proxy URL userinfo is sent only as Basic `Proxy-Authorization` to the proxy
(on CONNECT for HTTPS), and never logged or sent through the tunnel.

The default limit is 100 MiB per attachment. An oversized Content-Length is
rejected immediately; bodies stream in 64 KiB chunks with the same byte limit.
The socket timeout defaults to 30 seconds and the total attachment deadline to
300 seconds. Temporary `.part` files are removed on failure and renamed only
after a complete download. A rejected or failed attachment is logged without
URL userinfo, queries or fragments and skipped; remaining attachments and the
text message continue normally. Environment values take precedence over the
matching extra keys for all six inbound download settings.

## `media_local_roots` no longer scopes outbound delivery

`media_local_roots` used to be an allowlist of directories a local file could be
uploaded from. It is still parsed and still threaded through to
`media_runtime.ensure_allowed_local_path`, but that function accepts the value
for backward compatibility only and **does not restrict delivery** any more —
eligible local files may be sent from any directory.

What is actually enforced is a fixed **credential / system-path denylist** in
`clawchat_gateway/media_runtime.py`, applied after symlinks are resolved so it
cannot be bypassed via a symlink:

- absolute prefixes `/etc`, `/proc`, `/sys`, `/dev`, `/root`;
- paths under the user's home: `.ssh`, `.aws`, `.gnupg`, `.kube`,
  `.config/gcloud`, `.hermes/.env`;
- each resolved Hermes home and its named profiles: the `clawchat/` plugin data
  directory, `.env`, `config.yaml`, `auth.json`, and plugin database files
  (including SQLite sidecars and existing stores at custom paths);
- the Windows equivalents resolved from the environment (`%SystemRoot%` /
  `%WINDIR%`, and the Credential Manager / DPAPI / Vault directories under
  `%APPDATA%` and `%LOCALAPPDATA%`).

A path that matches the denylist is rejected with a neutral `ValueError` in all
turns; ordinary local files remain available in the owner's direct chat or on a
local operator surface, regardless of `media_local_roots`.

## Known limits of the attachment policy

The plugin judges attachments by the ClawChat turn they come from. Where no
ClawChat turn is in play, it does not restrict further:

- A delivery made with no session context and no trusted live turn nonce (Hermes sends a turn's reply after
  clearing the turn's context; cron delivery) may carry attachments into the
  owner's direct chat, local files and http(s) URLs alike, fetched the ordinary
  way. The only recipient there is the owner. Into any other chat it carries
  no local file. A trusted live reply nonce can retain only that turn's Files
  or artifact permission; a released nonce cannot recover it.
- A `send_message` call from another platform's own session, or with no session
  context, to a non-ClawChat target is left to Hermes and that platform: the
  plugin does not decide what another platform's turns may send.
- Hermes itself checks that a `MEDIA:` path exists before the plugin sees the
  call. The plugin applies its attachment policy afterwards; for a refused
  attachment nothing is read or sent, but that existence check has already happened.

