"""Simulate a Lemon Squeezy order_created webhook to grant 100 credits.

Useful for local testing — produces a real HMAC-signed request that the
production webhook handler accepts. Identical to what Lemon Squeezy will send
once you wire up the dashboard.

Usage:
    python scripts/topup.py                  # default: demo@example.com
    python scripts/topup.py alice@x.com      # custom email
"""
import hashlib
import hmac
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"


def _load_env():
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def main():
    _load_env()
    secret = os.environ.get("LEMON_SQUEEZY_WEBHOOK_SECRET")
    if not secret:
        sys.exit("LEMON_SQUEEZY_WEBHOOK_SECRET is not set in .env")

    base_url = os.environ.get("APP_URL", "http://127.0.0.1:8000")
    email = sys.argv[1] if len(sys.argv) > 1 else "demo@example.com"

    payload = {
        "meta": {
            "event_name": "order_created",
            "event_id": f"evt_local_{uuid.uuid4().hex[:12]}",
        },
        "data": {
            "type": "orders",
            "id": str(uuid.uuid4().int)[:12],
            "attributes": {
                "user_email": email,
                "total": 1000,
                "status": "paid",
            },
        },
    }
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    req = Request(
        f"{base_url}/api/webhooks/lemonsqueezy",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": sig,
        },
        method="POST",
    )

    print(f"-> POST {req.full_url}")
    print(f"   email     = {email}")
    print(f"   event_id  = {payload['meta']['event_id']}")
    try:
        with urlopen(req, timeout=10) as resp:
            response = json.loads(resp.read().decode())
            print(f"<- HTTP {resp.status}")
            print(json.dumps(response, indent=2))
    except HTTPError as e:
        print(f"<- HTTP {e.code}")
        print(e.read().decode(errors="replace"))
        sys.exit(1)
    except URLError as e:
        sys.exit(f"connection failed: {e.reason} (is uvicorn running on {base_url}?)")


if __name__ == "__main__":
    main()
