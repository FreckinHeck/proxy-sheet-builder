# artproxy API

A single-file Python server with no dependencies beyond the standard library
(Pillow is optional, and only improves `/thumb`). It exists because browsers
will not let a page read pixels from an image whose host sends no CORS headers
— which rules out reading Google Drive and the MPC Autofill CDN directly, and
therefore rules out exporting them into a PDF client-side. A server-side fetch
has no such restriction, so this fetches the bytes and re-serves them with a
permissive header.

## Authentication

Two separate tokens, with deliberately different trust levels.

**`PROXY_TOKEN`** gates the image and search endpoints, passed as `?k=<token>`.
It is written into `web/proxy-config.json`, which the browser fetches, so
**it is readable by anyone who views source**. Treat it as public. Its job is
to stop drive-by scrapers from spending your bandwidth and your Google Drive
rate limit, not to keep secrets. Rotate it whenever you like; the only cost is
updating `proxy-config.json`.

**`ADMIN_TOKEN`** gates order listing, passed as `?a=<token>` or the
`X-Admin-Token` header. This one is genuinely secret and must never appear in
`proxy-config.json` or anywhere else the page can reach. Without it set, the
admin endpoints refuse every request rather than defaulting open.

## Endpoints

### `GET /health`

No auth. Returns whether the server is up and how it is configured.

```json
{"ok": true, "backend": "https://mpcfill.com", "auth": true, "thumbs": true}
```

`thumbs: false` means Pillow is missing, so `/thumb` is serving originals and
you are moving far more data than you need to.

### `GET /img?u=<url encoded>&k=<token>`

Fetches an image and re-serves it with CORS. Only hosts in `ALLOWED_HOSTS`
are fetched — this is an allowlist, not an open relay, which matters a great
deal if the proxy is internet-reachable. Google Drive `/file/d/<id>` links are
rewritten to the direct download form automatically. Responses are cached on
disk up to `CACHE_MAX_MB`.

### `GET /thumb?id=<drive id>&w=<px>&k=<token>`

The bandwidth win, and worth understanding before you change it.

Naively, a thumbnail means fetching the original and resizing it. Drive
originals for MPC art run 5–15 MB, so opening the art window on a basic land
— 300 results — meant pulling roughly 3 GB through a home connection to
produce a grid of 400 px tiles.

Instead this hits Google's own thumbnail endpoint:

```
https://drive.google.com/thumbnail?id=<id>&sz=w<width>
```

Google renders and serves the tile itself, which is around 500× less data for
a typical grid. Pillow is only used as a fallback path. Thumbnails also get
their own wider concurrency lane (`THUMB_CONCURRENCY`, default 12) because
they are small enough that Drive does not throttle them the way it throttles
parallel full-file downloads (`CONCURRENCY`, default 4).

Pass `w` from `devicePixelRatio × tile width` so retina screens get sharp
tiles and everyone else does not pay for pixels they cannot see.

### `GET /drive?id=<file id>&k=<token>`

A Google Drive file by id, full size, with CORS. This is what the PDF export
path uses once a specific art has actually been chosen.

### `GET|POST /api/<path>?k=<token>`

Transparently proxied to `MPC_BACKEND` (default `https://mpcfill.com`),
preserving method and body. The MPC Autofill endpoints the builder uses:

| Endpoint             | Purpose                                  |
| -------------------- | ---------------------------------------- |
| `GET  /2/sources/`   | the list of contributor drives           |
| `POST /2/editorSearch/` | search across selected drives         |
| `POST /2/cards/`     | metadata for specific card ids           |

## Sheet requests

The four `order` endpoints let someone send you a finished sheet to print, so
whoever owns the printer is not rebuilding other people's decklists by hand.
The word "order" is just what the endpoints are called; there is no account
system, no payment handling and nothing stored about the sender beyond the
name they type in.

### `POST /order`

Accepts a submitted sheet. Returns its id and a per-sheet `edit_key`
(96 bits of `os.urandom`). Pings `WEBHOOK_URL` if one is configured.

Bounded by `ORDER_MAX_BYTES` and `ORDER_MAX_COUNT` so a submission flood
cannot fill the disk.

### `GET /order?id=<id>&key=<edit key>`

Reads one order back. Requires only the order's own key, so a submitter can
be handed a link to their own order without an account existing anywhere.
Strips `from_ip` and `admin_note` from the response — the person who sent the
sheet sees their own sheet, not your notes about it.

### `POST /order?id=<id>&key=<edit key>`

Updates that order in place. Preserves the original `received` timestamp and
adds `updated`.

### `POST /order/status`

Admin only. Moves an order between states (`new` / `done`).

### `GET /orders?a=<admin token>`

Admin only. Lists the most recent 100 orders.

> Note: the 100-order cap is on the listing, not on storage. Completed orders
> are folded into a collapsed section in the UI rather than deleted, so on a
> busy instance they will eventually push active orders out of the listing
> window. Raising the cap or filtering server-side by status is the fix if you
> get that far.

## Exposing it

The allowlist means this is not an open proxy, but it is still a service that
spends your bandwidth on request. If you put it on the internet:

- set `PROXY_TOKEN`, without exception
- put it behind a reverse proxy with TLS
- cache `/drive` and `/img` at the edge if you have Cloudflare in front
- consider an access layer (Cloudflare Access or similar) if the page is only
  meant for people you know — the proxy token cannot do that job, because the
  browser has to be able to read it

Running it on a LAN or tailnet and keeping it there is the low-stress option.
