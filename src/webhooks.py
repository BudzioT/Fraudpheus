import os, json, hmac, hashlib, threading, time
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv();

WEBHOOK_URLS = [u.strip() for u in os.getenv("FRAUDPHEUS_WEBHOOK_URLS", "").split(",") if u.strip()]
WEBHOOK_SECRET = os.getenv("FRAUDPHEUS_WEBHOOK_SECRET", "")
RETRY_DELAY = 5
MAX_ATTEMPTS = 3

def _sign(body_bytes):
    if not WEBHOOK_SECRET:
        return ""
    return hmac.new(WEBHOOK_SECRET.encode(), body_bytes, hashlib.sha256).hexdigest()

def _deliver(url, body_bytes, headers):
    attempt = 0
    while attempt < MAX_ATTEMPTS:
        try:
            resp = requests.post(url, data=body_bytes, headers=headers, timeout=10)
            if 200 <= resp.status_code < 300:
                return
        except Exception:
            pass
        attempt += 1
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY)

def dispatch_event(event_type, data):
    if not WEBHOOK_URLS:
        return
    payload = {"event_type": event_type, "data": data}
    body_bytes = json.dumps(payload, separators=(",", ":")).encode()
    signature = _sign(body_bytes)
    headers = {
        "Content-Type": "application/json",
        "X-Fraudpheus-Signature": signature
    }
    for url in WEBHOOK_URLS:
        threading.Thread(target=_deliver, args=(url, body_bytes, headers), daemon=True).start()