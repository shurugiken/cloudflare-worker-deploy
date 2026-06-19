# cloudflare-worker-deploy

Deploy a static HTML site as a Cloudflare Worker via the API — wrap a static
page into a Worker module and upload it programmatically. No `wrangler`, no
build server, no storage bucket.

Two small stdlib-only Python scripts:

- **`build_worker.py`** — wraps a static HTML file into a `worker.mjs` ES
  module that serves the page from the edge. Optionally inlines local assets
  (images, etc.) as base64 data-URIs so the Worker is a single self-contained
  file.
- **`deploy.py`** — uploads `worker.mjs` to Cloudflare with a multipart
  `PUT` to the Workers Scripts API, reading credentials from environment
  variables.

## Quick start

```bash
# 1. Build the Worker module from the sample page
python build_worker.py --html sample/index.html --out worker.mjs --inline-assets

# 2. Set credentials (see "Credentials" below)
export CF_ACCOUNT_ID=your_account_id
export CF_API_TOKEN=your_scoped_token

# 3. Deploy
python deploy.py --name my-site --module worker.mjs
```

After deploy the Worker is live at `https://my-site.<your-subdomain>.workers.dev`.

## Why serve a static site as a Worker?

For a small static page, a Worker is often the simplest thing that works:

- **One deployable.** The HTML lives inside the Worker script. There is no
  separate bucket, no asset upload step, no origin server to keep alive.
- **Edge cache.** The response ships with a `cache-control` header and runs at
  Cloudflare's edge, so it is fast everywhere without extra setup.
- **Programmatic.** The whole thing is a single API call, which makes it easy
  to drop into CI or a script. No `wrangler` install required.

This is a good fit for landing pages, status pages, and small single-file
sites. It is not meant for large multi-page sites with many assets — for that,
static hosting (Cloudflare Pages or R2) is the better tool.

## The build step

`build_worker.py` does three things:

1. **Reads the HTML** from `--html` (default `index.html`).
2. **Optionally inlines assets** when `--inline-assets` is passed. It scans
   `src=` / `href=` attributes, and for any reference that points at a local
   file that exists on disk, it replaces the reference with a
   `data:<mime>;base64,<...>` URI. Remote URLs, existing data-URIs, anchors,
   and missing files are left untouched. The result is a Worker with no
   external asset dependencies.
3. **Escapes and wraps** the HTML into a JS template literal and writes the
   module. Escaping order matters: backslashes first, then backticks, then the
   `${` interpolation sequence — otherwise the template literal breaks.

The generated `worker.mjs` looks like:

```js
const HTML = `...escaped html...`;

export default {
  async fetch(request) {
    return new Response(HTML, {
      headers: {
        "content-type": "text/html; charset=UTF-8",
        "cache-control": "public, max-age=300",
      },
    });
  },
};
```

## The deploy step

`deploy.py` uploads the module with a multipart `PUT`:

```
PUT /accounts/{account_id}/workers/scripts/{script_name}
```

The multipart body has two parts:

- a **`metadata`** part: JSON declaring `"main_module": "worker.mjs"` so
  Cloudflare knows the entrypoint is an ES module (not the older
  service-worker format), plus a `compatibility_date`.
- the **module file** part, with the form field name matching the
  `main_module` filename and a `Content-Type` of
  `application/javascript+module`.

The script prints `success` with the script id, or the structured error codes
Cloudflare returns on failure.

## Gotcha: custom domains need a Workers *Custom Domain*, not a CNAME

This one costs people an afternoon. To put your Worker on your own domain
(e.g. `www.example.com`), you must attach the hostname as a Workers **Custom
Domain**. That action provisions the route *and* the edge SSL certificate and
wires the hostname to the Worker.

A plain `CNAME` record pointing your domain at `my-site.workers.dev` does
**not** work — it returns **HTTP 522** (connection timed out), because
`*.workers.dev` is not an origin you can CNAME to.

Add a Custom Domain either way:

- **Dashboard:** Workers & Pages → your Worker → **Settings** → **Domains &
  Routes** → **Add** → **Custom Domain** → enter the hostname. The domain's
  zone must be on your Cloudflare account. Cloudflare creates the DNS record
  and provisions the certificate automatically (allow a few minutes for SSL).
- **API:**

  ```bash
  curl -X PUT \
    "https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}/workers/domains/records" \
    -H "Authorization: Bearer ${CF_API_TOKEN}" \
    -H "Content-Type: application/json" \
    --data '{
      "hostname": "www.example.com",
      "service": "my-site",
      "environment": "production",
      "zone_id": "your_zone_id"
    }'
  ```

## Credentials

- Create a **scoped API token** at Cloudflare → My Profile → API Tokens.
  The minimum permission is **Account → Workers Scripts → Edit**. (Adding a
  Custom Domain via the API also needs **Zone → Workers Routes → Edit** on the
  target zone.)
- Provide it to the scripts via environment variables — never hardcoded,
  never committed:

  ```bash
  export CF_ACCOUNT_ID=your_account_id
  export CF_API_TOKEN=your_scoped_token
  ```

`deploy.py` reads these from the environment and exits with a clear message if
either is missing. The `.gitignore` ignores `.env` and the build output so
secrets and generated files stay out of the repo.

## Files

| File | Purpose |
| --- | --- |
| `build_worker.py` | Wrap static HTML into `worker.mjs` (optional asset inlining) |
| `deploy.py` | Upload `worker.mjs` to Cloudflare via the API |
| `sample/index.html` | A tiny demo page so the repo runs end-to-end |

## Requirements

Python 3.8+. Standard library only — no third-party packages.

## License

MIT — see [LICENSE](LICENSE).
