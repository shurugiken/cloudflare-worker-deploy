#!/usr/bin/env python3
"""Upload a Worker module to Cloudflare via the REST API (no wrangler).

Reads credentials strictly from environment variables:
    CF_ACCOUNT_ID  -- your Cloudflare account ID
    CF_API_TOKEN   -- a scoped API token (Account > Workers Scripts: Edit)

Performs a multipart/form-data PUT to
    /accounts/{account_id}/workers/scripts/{script_name}
with a metadata part declaring `main_module`, then prints the API response.

Usage:
    python deploy.py --name my-site --module worker.mjs
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

API_BASE = "https://api.cloudflare.com/client/v4"


def read_creds():
    """Return (account_id, api_token) from env, or exit with a clear message."""
    account_id = os.environ.get("CF_ACCOUNT_ID")
    api_token = os.environ.get("CF_API_TOKEN")
    missing = [
        name
        for name, value in (("CF_ACCOUNT_ID", account_id), ("CF_API_TOKEN", api_token))
        if not value
    ]
    if missing:
        sys.exit(
            "error: missing required environment variable(s): "
            + ", ".join(missing)
            + "\nSet them before running, e.g.:\n"
            "    export CF_ACCOUNT_ID=your_account_id\n"
            "    export CF_API_TOKEN=your_scoped_token"
        )
    return account_id, api_token


def build_multipart(module_filename, module_source):
    """Build a multipart/form-data body for the Workers upload.

    The body contains:
      - a `metadata` part: JSON declaring the entrypoint as a module
      - the module file part itself, typed as application/javascript+module
    """
    boundary = "----worker-deploy-" + uuid.uuid4().hex
    metadata = {
        "main_module": module_filename,
        "compatibility_date": "2024-01-01",
    }

    parts = []

    # metadata part
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        b'Content-Disposition: form-data; name="metadata"\r\n'
        b"Content-Type: application/json\r\n\r\n"
    )
    parts.append(json.dumps(metadata).encode("utf-8"))
    parts.append(b"\r\n")

    # module part -- the form field name must match `main_module` above
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        (
            f'Content-Disposition: form-data; name="{module_filename}"; '
            f'filename="{module_filename}"\r\n'
            "Content-Type: application/javascript+module\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(module_source.encode("utf-8"))
    parts.append(b"\r\n")

    parts.append(f"--{boundary}--\r\n".encode())

    body = b"".join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def deploy(account_id, api_token, script_name, module_path):
    if not os.path.isfile(module_path):
        sys.exit(f"error: module file not found: {module_path}")

    module_filename = os.path.basename(module_path)
    with open(module_path, "r", encoding="utf-8") as fh:
        module_source = fh.read()

    body, content_type = build_multipart(module_filename, module_source)

    url = f"{API_BASE}/accounts/{account_id}/workers/scripts/{script_name}"
    request = urllib.request.Request(url, data=body, method="PUT")
    request.add_header("Authorization", f"Bearer {api_token}")
    request.add_header("Content-Type", content_type)

    try:
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        # Cloudflare returns structured JSON errors even on 4xx/5xx.
        raw = err.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            sys.exit(f"error: HTTP {err.code} from Cloudflare:\n{raw}")
    except urllib.error.URLError as err:
        sys.exit(f"error: could not reach Cloudflare API: {err.reason}")

    return payload


def report(payload, script_name):
    if payload.get("success"):
        print(f"success: deployed Worker '{script_name}'")
        result = payload.get("result") or {}
        if result.get("id"):
            print(f"  script id: {result['id']}")
        return 0

    print("failed: Cloudflare reported errors:", file=sys.stderr)
    for err in payload.get("errors", []) or []:
        code = err.get("code", "?")
        message = err.get("message", "unknown error")
        print(f"  [{code}] {message}", file=sys.stderr)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Upload a Worker module to Cloudflare via the API."
    )
    parser.add_argument(
        "--name",
        required=True,
        help="the Worker script name (becomes <name>.<subdomain>.workers.dev)",
    )
    parser.add_argument(
        "--module",
        default="worker.mjs",
        help="path to the Worker module to upload (default: worker.mjs)",
    )
    args = parser.parse_args(argv)

    account_id, api_token = read_creds()
    payload = deploy(account_id, api_token, args.name, args.module)
    sys.exit(report(payload, args.name))


if __name__ == "__main__":
    main()
