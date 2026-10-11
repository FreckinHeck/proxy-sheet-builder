# Proxy Sheet Builder

A self-hosted web tool for laying out Magic: The Gathering playtest proxies
into print-ready PDFs, with art pulled from [Scryfall](https://scryfall.com)
and [MPC Autofill](https://mpcfill.com).

Paste a decklist, pick art per card, get a PDF with exact-millimetre geometry
and Silhouette registration marks that actually register. Runs entirely on
your own hardware.

```
┌──────────────┬────────────────────────────┬──────────────┐
│  decklist    │                            │  page setup  │
│  import      │      card grid             │  presets     │
│  art source  │      (click a card         │  geometry    │
│  drive       │       to change its art)   │  marks       │
│  filters     │                            │  guides      │
└──────────────┴────────────────────────────┴──────────────┘
```

## Why it exists

Existing browser-based proxy builders work well on a desktop and poorly or not
at all on a phone, and none of them let you self-host. This one is a single
static HTML file plus a small Python CORS shim, so it runs on a NAS, a Pi, or
anything that can serve a file and run Python 3.

## What it does

**Import**
- Paste a decklist in the usual `4 Lightning Bolt` format
- Scryfall lookup with `include:extras` and all printings
- Double-faced cards produce both faces, not two copies of the front

**Art selection**
- Browse every printing from Scryfall, or every community art from MPC Autofill
- MPC search spans all contributor drives, paged — a basic land returns
  hundreds of options rather than an arbitrary first handful
- Per-copy art: four copies of a card can each carry different art, or apply
  one choice to all copies at once
- DPI shown per art so you can avoid the low-resolution uploads
- Filters for AI-generated art and non-English printings
- Pick a specific drive, or turn individual drives off

**Print**
- Letter and A4, 8-up (4×2) with Silhouette registration marks
- 9-up (3×3) with rotary-cutter guide lines for hand trimming
- 0.6 mm bleed, +10 % contrast, every value adjustable
- PDF export via jsPDF at exact millimetres — no scaling anywhere
- Works on mobile

**Session**
- Work persists across refresh via `localStorage`
- Optional request submission, so friends can send you a sheet to print

## Sending a request

If you are the one with the printer and the cutter, your friends probably do
not also want to own a printer and a cutter. The request feature exists for
that: they build a sheet themselves, send it to you, and you print it.

How it works:

1. A friend opens your instance, pastes their decklist, and picks art.
2. They hit submit and add a name so you know whose sheet it is.
3. Your webhook fires — a Discord or Slack ping with the sheet details, so you
   do not have to watch for anything.
4. They get a link back with a key in it. That link is theirs: it reopens the
   sheet so they can change art, fix a card they typo'd, or add something they
   forgot, right up until you print it.
5. You open the admin view, print the sheet, and mark it done. Finished sheets
   collapse into a dropdown rather than being deleted, so you can still look
   one up later.

The point is to save the back-and-forth. Without it, getting a sheet made for
someone means a decklist in a chat message, a round of "which art did you want
for the Sol Ring", and you rebuilding their sheet by hand. With it, they do
the fiddly part themselves and you just print.

There are no accounts. The order key is the only credential, it is 96 bits of
`os.urandom`, and it is only good for that one sheet — no email, no password,
nothing stored about anybody. Holding a key lets you read and edit that sheet
and nothing else.

Leave `WEBHOOK_URL` unset and the feature is inert, if you are only ever
printing your own sheets.

## Quick start

### Docker Compose

```bash
git clone https://github.com/FreckinHeck/proxy-sheet-builder.git
cd proxy-sheet-builder/deploy

cp artproxy.env.example artproxy.env
# Generate the two tokens and paste them in:
openssl rand -hex 24   # PROXY_TOKEN
openssl rand -hex 24   # ADMIN_TOKEN

docker compose up -d
```

Then point the page at the proxy:

```bash
cd ../web
cp proxy-config.example.json proxy-config.json
# Set "proxy" to your artproxy URL and "token" to PROXY_TOKEN
```

The builder is on `http://localhost:8080`, the proxy on `http://localhost:8778`.
Check `http://localhost:8778/health` first — it should report `"ok": true` and
`"thumbs": true`.

### TrueNAS Scale

See [`deploy/truenas-app.yaml`](deploy/truenas-app.yaml). Two things will cost
you an hour if you do not know them: every `volumes` entry must be a plain
`host:container` string (the long-form mapping syntax is silently discarded by
the YAML importer), and the host directories must exist and be owned by
`568:568` before you deploy.

### Anything else

`web/` is static — serve it with any web server. `server/artproxy.py` needs
only Python 3.9+; Pillow is optional and only improves thumbnail bandwidth.

## Configuration

`web/proxy-config.json` is read at page load, which keeps your proxy URL and
token out of the HTML and means updating the page never wipes your settings.
It is gitignored.

```json
{
  "proxy": "https://art.example.com",
  "token": "the value of PROXY_TOKEN",
  "operator": "Alex"
}
```

`operator` is optional and purely cosmetic — it is the name shown to whoever
submits a sheet, so the page can say "Alex prints it and gets back to you"
instead of "whoever runs this". Leave it empty and the copy stays impersonal.

Everything else is a UI setting, stored per browser.

Server configuration is entirely environment variables — see
[`deploy/artproxy.env.example`](deploy/artproxy.env.example) and
[`docs/api.md`](docs/api.md).

## How it fits together

```
browser ──► index.html ──┬──► api.scryfall.com          (CORS-friendly, direct)
                         │
                         └──► artproxy ──┬──► mpcfill.com      /api/*
                                         ├──► drive.google.com /drive, /thumb
                                         └──► disk cache
```

Scryfall sends CORS headers, so the page talks to it directly. Google Drive
and the MPC CDN do not, which means a browser cannot read their pixels into a
canvas and therefore cannot get them into a PDF. `artproxy` fetches those
server-side and re-serves them with a permissive header. It fetches only from
an allowlist of hosts — it is not an open relay.

Thumbnails go through Google's own `drive.google.com/thumbnail` endpoint rather
than downloading 5–15 MB originals to resize locally. For a 300-result art
grid that is roughly a 500× difference in bytes moved, which is the difference
between usable and unusable on a home connection.

## Documentation

- [`docs/print-geometry.md`](docs/print-geometry.md) — card dimensions, bleed,
  the MPC canvas crop, and Silhouette registration mark geometry. Read this
  before changing any number that affects the page layout.
- [`docs/api.md`](docs/api.md) — every artproxy endpoint, both tokens and what
  they are each good for.

## Intended use

This builds **playtest proxies** — cards for casual games, for testing a deck
before buying into it, and for playing with expensive cards without carrying
expensive cards. That is the use case it was written for and the only one it
is documented for.

It is not a counterfeiting tool and should not be used as one. Proxies made
with it print on consumer paper at consumer resolution, carry whatever art the
community uploaded, and will not pass for authentic cards in any setting where
that matters. Do not represent them as real cards, do not sell them as real
cards, and do not bring them to sanctioned play.

Art belongs to its creators and to Wizards of the Coast. Community art hosted
on MPC Autofill belongs to whoever uploaded it, and you should respect the
terms of both MPC Autofill and Scryfall when using their data.

## Credits

- [Scryfall](https://scryfall.com) for the card database and API
- [MPC Autofill](https://mpcfill.com) for the community art ecosystem
- [jsPDF](https://github.com/parallax/jsPDF) for PDF generation
- [proxxied.com](https://proxxied.com) for the three-panel layout this borrows

## Disclaimer

Unofficial Fan Content. Not approved or endorsed by Wizards of the Coast.
Portions of the materials used are property of Wizards of the Coast.
© Wizards of the Coast LLC.

## License

[MIT](LICENSE)
