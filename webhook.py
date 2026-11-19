"""WhatsApp Cloud API adapter. Serve app behind an HTTPS WSGI host."""
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import psycopg
from psycopg.rows import dict_row
import time
from contextlib import ExitStack, closing
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from bot import OrderChat

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
MENU_ASSETS = Path(__file__).with_name("menu-assets")
EMBEDDED_SIGNUP_PAGE = Path(__file__).with_name("embedded_signup.html")
META_APP_ID_DEFAULT = "2215718912674205"
META_GRAPH_VERSION_DEFAULT = "v27.0"
META_EMBEDDED_SIGNUP_CONFIG_ID_DEFAULT = "1553383993216831"
WHALEADS_BASE_URL_DEFAULT = "https://ada-place-grill-inbox.eyoleabubakar.workers.dev"
COEXISTENCE_FIELDS = {"account_update", "history", "smb_app_state_sync", "smb_message_echoes"}


class MetaAPIError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def graph_request(path, access_token, params=None, method="GET"):
    version = os.environ.get("META_GRAPH_VERSION", META_GRAPH_VERSION_DEFAULT)
    values = dict(params or {})
    headers = {"Authorization": "Bearer " + access_token} if access_token else {}
    if method == "GET":
        url = f"https://graph.facebook.com/{version}/{path}?{urlencode(values)}"
        request = Request(url, headers=headers)
    else:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(
            f"https://graph.facebook.com/{version}/{path}",
            data=urlencode(values).encode(), headers=headers, method=method,
        )
    try:
        with urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            payload = {}
        error = payload.get("error", {})
        raise MetaAPIError(error.get("message", "Meta API request failed"), error.get("code")) from None
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise MetaAPIError("Meta API request could not be completed") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        raise MetaAPIError(error.get("message", "Meta API returned an invalid response"), error.get("code"))
    return payload


def exchange_signup_code(code):
    app_id = os.environ.get("META_APP_ID", META_APP_ID_DEFAULT)
    app_secret = os.environ.get("META_APP_SECRET") or os.environ["WA_APP_SECRET"]
    return graph_request(
        "oauth/access_token", "", {
            "client_id": app_id,
            "client_secret": app_secret,
            "code": code,
        },
    )


def _number_id(value):
    return isinstance(value, str) and value.isascii() and value.isdecimal() and 5 <= len(value) <= 30


def complete_coexistence_signup(data):
    if not isinstance(data, dict):
        raise ValueError("The signup session data is missing.")
    session = data.get("session_info")
    code = data.get("code")
    if not isinstance(session, dict) or not isinstance(code, str) or not code or len(code) > 4096:
        raise ValueError("Meta did not return a complete signup session.")
    if session.get("type") != "WA_EMBEDDED_SIGNUP":
        raise ValueError("This is not a WhatsApp Embedded Signup session.")
    event = session.get("event")
    details = session.get("data")
    if not isinstance(details, dict) or event != "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING":
        raise ValueError("Meta did not finish the WhatsApp Business app connection flow.")
    if details.get("is_on_biz_app") is not True:
        raise ValueError("Meta did not confirm WhatsApp Business app coexistence.")
    phone_id = details.get("phone_number_id")
    messaging_ids = details.get("messaging_account_ids") or []
    if not messaging_ids and details.get("waba_id"):
        messaging_ids = [details["waba_id"]]
    if not _number_id(phone_id) or not messaging_ids or not _number_id(str(messaging_ids[0])):
        raise ValueError("Meta did not return the connected phone and messaging account IDs.")
    # Coexistence can connect a different WhatsApp Business app number.
    # The existing runtime config remains unchanged until the new number is verified.

    token_result = exchange_signup_code(code)
    business_token = token_result.get("access_token")
    if not isinstance(business_token, str) or not business_token:
        raise MetaAPIError("Meta did not return an access token.")

    phone_status = graph_request(
        phone_id, business_token,
        {"fields": "is_on_biz_app,platform_type"},
    )
    if phone_status.get("is_on_biz_app") is not True or phone_status.get("platform_type") != "CLOUD_API":
        raise ValueError("Meta has not confirmed that this number is active in both the Business app and Cloud API.")

    runtime_token = os.environ.get("WA_ACCESS_TOKEN", "")
    bot_ready = False
    if runtime_token:
        try:
            graph_request(phone_id, runtime_token, {"fields": "id"})
            bot_ready = True
        except MetaAPIError:
            logger.warning("Coexistence connected, but the current bot token cannot access the new phone number")

    messaging_id = str(messaging_ids[0])
    graph_request(f"{messaging_id}/subscribed_apps", business_token, {}, method="POST")
    sync_results = []
    for sync_type in ("smb_app_state_sync", "history"):
        try:
            sync_results.append(graph_request(
                f"{phone_id}/smb_app_data", business_token,
                {"messaging_product": "whatsapp", "sync_type": sync_type}, method="POST",
            ))
        except MetaAPIError as exc:
            if sync_type == "history" and exc.code == 2593109:
                logger.info("Coexistence connected; business chose not to share WhatsApp history")
                sync_results.append({"history_shared": False})
                continue
            raise
    logger.info(
        "Coexistence onboarding completed: phone_id=%s messaging_account_id=%s app_number=%s",
        phone_id, messaging_id, phone_status.get("is_on_biz_app"),
    )
    return {
        "connected": True,
        "contacts_sync": bool(sync_results),
        "phone_number_id": phone_id,
        "bot_ready": bot_ready,
    }


def create_signup_csrf(secret):
    nonce = f"{int(time.time())}.{secrets.token_urlsafe(24)}"
    signature = hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return nonce, f"{nonce}.{signature}"


def valid_signup_csrf(environ, nonce):
    secret = os.environ.get("WA_APP_SECRET", "")
    cookie_header = environ.get("HTTP_COOKIE", "")
    cookie = next((part.strip().split("=", 1)[1] for part in cookie_header.split(";")
                   if part.strip().startswith("meta_signup_csrf=")), "")
    if not secret or not nonce or not cookie:
        return False
    try:
        cookie_nonce, signature = cookie.rsplit(".", 1)
        created = int(cookie_nonce.split(".", 1)[0])
    except ValueError:
        return False
    except (IndexError, TypeError):
        return False
    if abs(int(time.time()) - created) > 600:
        return False
    expected = hmac.new(secret.encode(), cookie_nonce.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(cookie_nonce, nonce) and hmac.compare_digest(signature, expected)


def coexistence_event_count(field, value):
    candidates = value.get("messages", [])
    if not candidates and field == "history":
        candidates = value.get("history", [])
    if not candidates and field == "smb_app_state_sync":
        candidates = value.get("contacts", [])
    return len(candidates) if isinstance(candidates, list) else 0


def process(db, message, inbox_db=None):
    message_id = message["id"]
    customer = message["from"]
    with db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT reply, image_url, sent FROM events WHERE id = ?", (message_id,)
        ).fetchone()
        if previous:
            return previous
        inbound_type = message.get("type", "unknown")
        payload = message.get(inbound_type, {})
        if inbound_type == "text":
            inbound_body = payload.get("body", "")
        elif inbound_type in {"image", "video", "audio", "document", "sticker"}:
            inbound_body = f"[{inbound_type} received]"
            caption = payload.get("caption")
            if caption:
                inbound_body += f" {caption}"
        else:
            inbound_body = f"[{inbound_type} message received]"
        human_mode = False
        if inbox_db is not None:
            with inbox_db.transaction():
                inbox_db.execute(
                    "INSERT INTO inbox_messages "
                    "(wa_message_id, customer, direction, body, created_at, status) "
                    "VALUES (%s, %s, 'inbound', %s, %s, 'received') "
                    "ON CONFLICT (wa_message_id) DO NOTHING",
                    (message_id, customer, inbound_body[:4096], int(time.time())),
                )
                human = inbox_db.execute(
                    "SELECT human_mode FROM inbox_handoffs WHERE customer = %s", (customer,)
                ).fetchone()
                human_mode = bool(human and human["human_mode"])
        if human_mode:
            return None, None, True
        if inbound_type != "text":
            return None, None, True
        chat = OrderChat(db, customer)
        session = db.execute(
            "SELECT state FROM sessions WHERE customer = ?", (customer,)
        ).fetchone()
        if session:
            state = json.loads(session[0])
            state["cart"] = {(name, price): qty for name, price, qty in state["cart"]}
            if "selected" in state:
                state["selected"] = tuple(state["selected"])
            vars(chat).update(state)
        reply = chat.reply(message["text"]["body"])
        image_url = None
        if chat.response_image:
            public_url = os.environ.get("WA_PUBLIC_URL", "https://adabot-rkes.onrender.com").rstrip("/")
            asset_path = MENU_ASSETS / chat.response_image
            image_version = hashlib.sha256(asset_path.read_bytes()).hexdigest()[:12] if asset_path.is_file() else "missing"
            image_url = f"{public_url}/menu-assets/{chat.response_image}?v={image_version}"
        state = {key: value for key, value in vars(chat).items()
                 if key not in {"database", "customer", "response_image"}}
        state["cart"] = [[name, price, qty] for (name, price), qty in chat.cart.items()]
        db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (customer, json.dumps(state)))
        db.execute("INSERT INTO events (id, reply, image_url, sent) VALUES (?, ?, ?, 0)",
                   (message_id, reply, image_url))
    return reply, image_url, False


def send_reply(phone_id, recipient, reply, image_url=None):
    version = os.environ["WA_GRAPH_VERSION"]
    if image_url:
        body = dict(messaging_product="whatsapp", to=recipient, type="image",
                    image={"link": image_url, "caption": reply})
    else:
        body = dict(messaging_product="whatsapp", to=recipient, type="text", text={"body": reply})
    request = Request(
        f"https://graph.facebook.com/{version}/{phone_id}/messages",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + os.environ["WA_ACCESS_TOKEN"],
                 "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=8) as response:
        try:
            result = json.loads(response.read().decode("utf-8"))
        except (ValueError, OSError):
            result = {}
        print(f"WhatsApp reply accepted: kind={'image' if image_url else 'text'} HTTP {response.status}", flush=True)
        messages = result.get("messages", []) if isinstance(result, dict) else []
        return messages[0].get("id") if messages and isinstance(messages[0], dict) else None


def _whaleads_url():
    return os.environ.get("WHALEADS_BASE_URL", WHALEADS_BASE_URL_DEFAULT).rstrip("/")


def forward_inbound_to_whaleads(raw, meta_signature):
    """Mirror Meta's signed webhook to WhaLeads while Render remains the Meta callback."""
    request = Request(
        _whaleads_url() + "/webhooks/meta",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "AdaPlaceGrillBotBridge/1.0",
            "X-Hub-Signature-256": meta_signature,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=4) as response:
            if response.status >= 300:
                logger.warning("WhaLeads inbound mirror returned HTTP %s", response.status)
    except HTTPError as exc:
        detail = exc.read(512).decode("utf-8", errors="replace").replace("\\n", " ")
        ray = exc.headers.get("CF-Ray", "") if exc.headers else ""
        logger.warning("WhaLeads inbound mirror failed: HTTP %s cf-ray=%s body=%r", exc.code, ray, detail)
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("WhaLeads inbound mirror failed: %s", exc)


def mirror_bot_reply(customer, wamid, body):
    """Mirror a successful bot reply with an HMAC accepted only by our WhaLeads bridge."""
    if not wamid:
        logger.warning("Bot reply was not mirrored because Meta returned no message ID")
        return
    payload = {
        "type": "bot_outbound",
        "customer": str(customer),
        "wamid": str(wamid),
        "body": str(body)[:4096],
        "timestamp": int(time.time()),
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signature = "sha256=" + hmac.new(os.environ["WA_APP_SECRET"].encode(), raw, hashlib.sha256).hexdigest()
    request = Request(
        _whaleads_url() + "/webhooks/render-bridge",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "AdaPlaceGrillBotBridge/1.0",
            "X-Ada-Bridge-Signature": signature,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=4) as response:
            if response.status >= 300:
                logger.warning("WhaLeads bot reply mirror returned HTTP %s", response.status)
    except HTTPError as exc:
        detail = exc.read(512).decode("utf-8", errors="replace").replace("\\n", " ")
        ray = exc.headers.get("CF-Ray", "") if exc.headers else ""
        logger.warning("WhaLeads bot reply mirror failed: HTTP %s cf-ray=%s body=%r", exc.code, ray, detail)
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("WhaLeads bot reply mirror failed: %s", exc)


def receive(event):
    phone_id = os.environ["WA_PHONE_NUMBER_ID"]
    entry_count = 0
    change_count = 0
    message_count = 0
    text_message_count = 0
    sent_count = 0
    coexistence_change_count = 0
    with ExitStack() as stack:
        db = stack.enter_context(closing(sqlite3.connect(os.environ["WA_DATABASE"])))
        try:
            inbox_db = inbox_connect()
            with inbox_db.transaction():
                ensure_inbox_schema(inbox_db)
            inbox_db = stack.enter_context(closing(inbox_db))
        except (KeyError, psycopg.Error):
            if "inbox_db" in locals() and inbox_db is not None:
                inbox_db.close()
            logger.exception("Staff inbox database is unavailable; continuing bot replies without inbox logging")
            inbox_db = None
        db.execute("CREATE TABLE IF NOT EXISTS sessions (customer TEXT PRIMARY KEY, state TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, reply TEXT, image_url TEXT, sent INTEGER)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS coexistence_webhook_events "
            "(id INTEGER PRIMARY KEY, field TEXT NOT NULL, entry_id TEXT, "
            "phone_number_id TEXT, item_count INTEGER NOT NULL, received_at INTEGER NOT NULL)"
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
        if "image_url" not in columns:
            db.execute("ALTER TABLE events ADD COLUMN image_url TEXT")
        for entry in event.get("entry", []):
            entry_count += 1
            for change in entry.get("changes", []):
                change_count += 1
                field = change.get("field", "unknown")
                value = change.get("value", {})
                if not isinstance(value, dict):
                    logger.warning("Ignoring malformed WhatsApp webhook value for field=%s", field)
                    continue
                metadata_phone_id = value.get("metadata", {}).get("phone_number_id")
                messages = value.get("messages", [])
                statuses = value.get("statuses", [])
                if not isinstance(messages, list):
                    messages = []
                if not isinstance(statuses, list):
                    statuses = []
                change_phone_id = metadata_phone_id if _number_id(metadata_phone_id) else None
                print(
                    f"Webhook change received: field={field} "
                    f"phone_id_valid={change_phone_id is not None} "
                    f"inbound_messages={len(messages)} statuses={len(statuses)}",
                    flush=True,
                )
                if field in COEXISTENCE_FIELDS:
                    item_count = coexistence_event_count(field, value)
                    with db:
                        db.execute(
                            "INSERT INTO coexistence_webhook_events "
                            "(field, entry_id, phone_number_id, item_count, received_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (field, str(entry.get("id", "")), metadata_phone_id,
                             item_count, int(time.time())),
                        )
                    coexistence_change_count += 1
                    logger.info(
                        "Coexistence webhook digested: field=%s entry_id=%s phone_number_id=%s items=%d",
                        field, entry.get("id", "unknown"), metadata_phone_id or "unknown", item_count,
                    )
                    continue
                if field != "messages":
                    logger.info("Ignoring unsupported WhatsApp webhook field=%s", field)
                    continue
                if change_phone_id is None:
                    logger.warning(
                        "Ignoring webhook change with missing or invalid phone number ID; messages=%d",
                        len(messages),
                    )
                    continue
                for message in messages:
                    message_count += 1
                    if message.get("type") == "text":
                        text_message_count += 1
                    reply, image_url, sent = process(db, message, inbox_db)
                    if not sent and reply is not None:
                        sent_wamid = send_reply(change_phone_id, message["from"], reply, image_url)
                        mirror_bot_reply(message["from"], sent_wamid, reply)
                        with db:
                            db.execute("UPDATE events SET sent = 1 WHERE id = ?", (message["id"],))
                        if inbox_db is not None:
                            with inbox_db.transaction():
                                inbox_db.execute(
                                    "INSERT INTO inbox_messages "
                                    "(wa_message_id, customer, direction, body, created_at, status) "
                                    "VALUES (%s, %s, 'outbound', %s, %s, 'sent') "
                                    "ON CONFLICT (wa_message_id) DO NOTHING",
                                    ("bot:" + message["id"], message["from"], reply[:4096], int(time.time())),
                                )
                        sent_count += 1
        logger.info(
            "Webhook processed: entries=%d changes=%d coexistence_changes=%d matching_messages=%d text_messages=%d replies_sent=%d",
            entry_count, change_count, coexistence_change_count, message_count, text_message_count, sent_count,
        )
    if change_count == 0 or (message_count == 0 and coexistence_change_count == 0):
        logger.info("Webhook contained no matching inbound text messages")


def inbox_connect():
    database_url = os.environ["INBOX_DATABASE_URL"]
    return psycopg.connect(database_url, row_factory=dict_row, connect_timeout=5)


def ensure_inbox_schema(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS inbox_messages ("
        "id BIGSERIAL PRIMARY KEY, wa_message_id TEXT UNIQUE, "
        "customer TEXT NOT NULL, direction TEXT NOT NULL, body TEXT NOT NULL, "
        "created_at BIGINT NOT NULL, status TEXT NOT NULL)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS inbox_messages_customer_time "
        "ON inbox_messages(customer, created_at)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS inbox_handoffs ("
        "customer TEXT PRIMARY KEY, human_mode BOOLEAN NOT NULL DEFAULT FALSE, "
        "updated_at BIGINT NOT NULL)"
    )


def inbox_authorized(environ):
    username = os.environ.get("INBOX_USERNAME", "")
    password = os.environ.get("INBOX_PASSWORD", "")
    if not username or len(password) < 20:
        return False, True
    header = environ.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Basic "):
        return False, False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        supplied_user, supplied_password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False, False
    ok = hmac.compare_digest(supplied_user, username) and hmac.compare_digest(supplied_password, password)
    return ok, False


def inbox_handle(environ, start_response, respond):
    path = environ["PATH_INFO"]
    if path != "/inbox" and not path.startswith("/inbox/"):
        return None
    authorized, not_configured = inbox_authorized(environ)
    if not_configured:
        return respond("503 Service Unavailable", "Staff inbox is not configured. Set INBOX_USERNAME and a strong INBOX_PASSWORD in the service environment.")
    if not authorized:
        return respond(
            "401 Unauthorized", "Sign in to the staff inbox.",
            extra_headers=[("WWW-Authenticate", 'Basic realm="Ada staff inbox", charset="UTF-8"')],
        )
    if environ["REQUEST_METHOD"] not in {"GET", "POST"}:
        return respond("405 Method Not Allowed", "Method not allowed")
    try:
        with closing(inbox_connect()) as db:
            with db.transaction():
                ensure_inbox_schema(db)
            if path == "/inbox" and environ["REQUEST_METHOD"] == "GET":
                page = Path(__file__).with_name("inbox.html").read_bytes()
                start_response("200 OK", [
                    ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"),
                    ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
                    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"),
                ])
                return [page]
            if path == "/inbox/api/conversations" and environ["REQUEST_METHOD"] == "GET":
                rows = db.execute(
                    "SELECT m.customer, m.body, m.created_at, m.direction, COALESCE(h.human_mode, FALSE) AS human_mode "
                    "FROM inbox_messages m LEFT JOIN inbox_handoffs h ON h.customer=m.customer "
                    "JOIN (SELECT customer, MAX(id) AS latest_id FROM inbox_messages GROUP BY customer) x "
                    "ON x.latest_id=m.id ORDER BY m.created_at DESC LIMIT 200"
                ).fetchall()
                return respond("200 OK", json.dumps([dict(row) for row in rows]), "application/json; charset=utf-8", [("Cache-Control", "no-store")])
            if path == "/inbox/api/messages" and environ["REQUEST_METHOD"] == "GET":
                customer = parse_qs(environ.get("QUERY_STRING", "")).get("customer", [""])[0]
                if not re.fullmatch(r"[0-9]{5,20}", customer):
                    return respond("400 Bad Request", '{"error":"Invalid customer number"}', "application/json")
                rows = db.execute(
                    "SELECT direction, body, created_at, status FROM inbox_messages WHERE customer = %s ORDER BY created_at, id LIMIT 1000",
                    (customer,),
                ).fetchall()
                handoff = db.execute("SELECT human_mode FROM inbox_handoffs WHERE customer = %s", (customer,)).fetchone()
                return respond("200 OK", json.dumps({
                    "customer": customer, "human_mode": bool(handoff and handoff["human_mode"]), "messages": [dict(row) for row in rows],
                }), "application/json; charset=utf-8", [("Cache-Control", "no-store")])
            if path not in {"/inbox/api/reply", "/inbox/api/handoff"} or environ["REQUEST_METHOD"] != "POST":
                return respond("404 Not Found", "Not found")
            origin = environ.get("HTTP_ORIGIN", "")
            public_host = urlsplit(os.environ.get("WA_PUBLIC_URL", "https://adabot-rkes.onrender.com")).netloc
            if not origin or urlsplit(origin).scheme != "https" or urlsplit(origin).netloc != public_host:
                return respond("403 Forbidden", '{"error":"Request origin was not accepted"}', "application/json")
            if not environ.get("CONTENT_TYPE", "").startswith("application/json"):
                return respond("415 Unsupported Media Type", '{"error":"JSON required"}', "application/json")
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
                if not 1 <= length <= 8192:
                    raise ValueError
                data = json.loads(environ["wsgi.input"].read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                return respond("400 Bad Request", '{"error":"Invalid request"}', "application/json")
            customer = str(data.get("customer", ""))
            if not re.fullmatch(r"[0-9]{5,20}", customer):
                return respond("400 Bad Request", '{"error":"Invalid customer number"}', "application/json")
            if path == "/inbox/api/handoff":
                human_mode = bool(data.get("human_mode"))
                with db.transaction():
                    db.execute(
                        "INSERT INTO inbox_handoffs(customer, human_mode, updated_at) VALUES (%s, %s, %s) "
                        "ON CONFLICT(customer) DO UPDATE SET human_mode=EXCLUDED.human_mode, updated_at=EXCLUDED.updated_at",
                        (customer, human_mode, int(time.time())),
                    )
                return respond("200 OK", json.dumps({"customer": customer, "human_mode": human_mode}), "application/json")
            message = data.get("message", "").strip()
            if not message or len(message) > 4096:
                return respond("400 Bad Request", '{"error":"Message must be 1–4096 characters"}', "application/json")
            send_reply(os.environ["WA_PHONE_NUMBER_ID"], customer, message)
            with db.transaction():
                db.execute(
                    "INSERT INTO inbox_handoffs(customer, human_mode, updated_at) VALUES (%s, TRUE, %s) "
                    "ON CONFLICT(customer) DO UPDATE SET human_mode=TRUE, updated_at=EXCLUDED.updated_at",
                    (customer, int(time.time())),
                )
                db.execute(
                    "INSERT INTO inbox_messages (wa_message_id, customer, direction, body, created_at, status) "
                    "VALUES (%s, %s, 'outbound', %s, %s, 'sent')",
                    ("staff:" + secrets.token_urlsafe(18), customer, message, int(time.time())),
                )
            return respond("200 OK", '{"sent":true}', "application/json")
    except (OSError, sqlite3.Error, psycopg.Error, KeyError):
        logger.exception("Staff inbox request failed")
        return respond("503 Service Unavailable", '{"error":"Inbox is temporarily unavailable"}', "application/json")


def app(environ, start_response):
    def respond(status, body, content_type="text/plain; charset=utf-8", extra_headers=()):
        start_response(status, [("Content-Type", content_type), *extra_headers])
        return [body if isinstance(body, bytes) else body.encode()]

    if environ["PATH_INFO"] == "/health" and environ["REQUEST_METHOD"] == "GET":
        return respond("200 OK", "OK")
    if environ["PATH_INFO"] == "/bridge/handoff":
        if environ["REQUEST_METHOD"] != "POST":
            return respond("405 Method Not Allowed", "Method not allowed")
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
            if not 1 <= length <= 8192 or not environ.get("CONTENT_TYPE", "").startswith("application/json"):
                return respond("400 Bad Request", '{"error":"Invalid request"}', "application/json")
            raw = environ["wsgi.input"].read(length)
            expected = "sha256=" + hmac.new(os.environ["WA_APP_SECRET"].encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, environ.get("HTTP_X_ADA_BRIDGE_SIGNATURE", "")):
                return respond("403 Forbidden", '{"error":"Invalid signature"}', "application/json")
            data = json.loads(raw.decode("utf-8"))
            customer = str(data.get("customer", ""))
            if not re.fullmatch(r"[0-9]{5,20}", customer) or data.get("human_mode") is not True:
                return respond("400 Bad Request", '{"error":"Invalid handoff"}', "application/json")
            with closing(inbox_connect()) as db:
                with db.transaction():
                    ensure_inbox_schema(db)
                    db.execute(
                        "INSERT INTO inbox_handoffs(customer, human_mode, updated_at) VALUES (%s, TRUE, %s) "
                        "ON CONFLICT(customer) DO UPDATE SET human_mode=TRUE, updated_at=EXCLUDED.updated_at",
                        (customer, int(time.time())),
                    )
            return respond("200 OK", '{"human_mode":true}', "application/json")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return respond("400 Bad Request", '{"error":"Invalid request"}', "application/json")
        except (OSError, psycopg.Error):
            logger.exception("Signed staff handoff failed")
            return respond("503 Service Unavailable", '{"error":"Handoff unavailable"}', "application/json")
    inbox_response = inbox_handle(environ, start_response, respond)
    if inbox_response is not None:
        return inbox_response
    if environ["PATH_INFO"] == "/connect-whatsapp":
        if environ["REQUEST_METHOD"] != "GET":
            return respond("405 Method Not Allowed", "Method not allowed")
        config_id = os.environ.get("META_EMBEDDED_SIGNUP_CONFIG_ID", META_EMBEDDED_SIGNUP_CONFIG_ID_DEFAULT)
        app_id = os.environ.get("META_APP_ID", META_APP_ID_DEFAULT)
        graph_version = os.environ.get("META_GRAPH_VERSION", META_GRAPH_VERSION_DEFAULT)
        if (not re.fullmatch(r"[0-9]{5,32}", config_id)
                or not re.fullmatch(r"[0-9]{5,32}", app_id)
                or not re.fullmatch(r"v[0-9]+\.[0-9]+", graph_version)
                or not os.environ.get("WA_APP_SECRET")):
            return respond(
                "503 Service Unavailable",
                "WhatsApp signup is not configured yet. Finish Meta's app review and add the Embedded Signup configuration ID in Render.",
            )
        nonce, csrf_cookie = create_signup_csrf(os.environ["WA_APP_SECRET"])
        page = EMBEDDED_SIGNUP_PAGE.read_text(encoding="utf-8")
        page = page.replace("__APP_ID__", json.dumps(app_id))
        page = page.replace("__CONFIG_ID__", json.dumps(config_id))
        page = page.replace("__GRAPH_VERSION__", json.dumps(graph_version))
        page = page.replace("__CSRF_TOKEN__", json.dumps(nonce))
        return respond(
            "200 OK", page, "text/html; charset=utf-8",
            [
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "same-origin"),
                ("Set-Cookie", f"meta_signup_csrf={csrf_cookie}; Path=/; Max-Age=600; Secure; HttpOnly; SameSite=Lax"),
            ],
        )
    if environ["PATH_INFO"] == "/embedded-signup/complete":
        if environ["REQUEST_METHOD"] != "POST":
            return respond("405 Method Not Allowed", "Method not allowed")
        origin = environ.get("HTTP_ORIGIN", "")
        public_origin = urlsplit(os.environ.get("WA_PUBLIC_URL", "https://adabot-rkes.onrender.com")).netloc
        if not origin or urlsplit(origin).scheme != "https" or urlsplit(origin).netloc != public_origin:
            return respond("403 Forbidden", json.dumps({"error": "Request origin was not accepted."}), "application/json")
        nonce = environ.get("HTTP_X_CSRF_TOKEN", "")
        if not valid_signup_csrf(environ, nonce):
            return respond("403 Forbidden", json.dumps({"error": "Signup session expired. Reload and try again."}), "application/json")
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
            if not 0 < length <= 16384:
                return respond("400 Bad Request", json.dumps({"error": "Invalid request size."}), "application/json")
            payload = json.loads(environ["wsgi.input"].read(length).decode("utf-8"))
            result = complete_coexistence_signup(payload)
            return respond("200 OK", json.dumps(result), "application/json", [("Cache-Control", "no-store")])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return respond("400 Bad Request", json.dumps({"error": str(exc)}), "application/json")
        except MetaAPIError as exc:
            logger.error("Meta Coexistence setup failed: code=%s", exc.code or "unknown")
            return respond("502 Bad Gateway", json.dumps({
                "error": "Meta could not complete Coexistence setup. The current bot token and number configuration were left unchanged.",
                "meta_error_code": exc.code,
            }), "application/json")
        except (OSError, sqlite3.Error):
            logger.exception("Coexistence signup handler failed")
            return respond("503 Service Unavailable", json.dumps({"error": "The setup could not be completed."}), "application/json")
    if environ["PATH_INFO"].startswith("/menu-assets/"):
        if environ["REQUEST_METHOD"] != "GET":
            return respond("405 Method Not Allowed", "Method not allowed")
        filename = environ["PATH_INFO"].removeprefix("/menu-assets/")
        if not filename or Path(filename).name != filename:
            return respond("404 Not Found", "Not found")
        asset = MENU_ASSETS / filename
        if not asset.is_file():
            return respond("404 Not Found", "Not found")
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with asset.open("rb") as image:
            data = image.read()
        start_response("200 OK", [("Content-Type", content_type),
                                   ("Content-Length", str(len(data))),
                                   ("Cache-Control", "public, max-age=86400")])
        return [data]
    if environ["PATH_INFO"] != "/webhook":
        return respond("404 Not Found", "Not found")
    if environ["REQUEST_METHOD"] == "GET":
        query = parse_qs(environ.get("QUERY_STRING", ""))
        token = query.get("hub.verify_token", [""])[0]
        if (query.get("hub.mode") == ["subscribe"] and "hub.challenge" in query
                and hmac.compare_digest(token.encode(), os.environ["WA_VERIFY_TOKEN"].encode())):
            return respond("200 OK", query["hub.challenge"][0])
        return respond("403 Forbidden", "Verification failed")
    if environ["REQUEST_METHOD"] != "POST":
        return respond("405 Method Not Allowed", "Method not allowed")
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if not 0 < length <= 1024 * 1024:
            return respond("400 Bad Request", "Invalid body length")
        raw = environ["wsgi.input"].read(length)
        expected = "sha256=" + hmac.new(
            os.environ["WA_APP_SECRET"].encode(), raw, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, environ.get("HTTP_X_HUB_SIGNATURE_256", "")):
            logger.warning("Rejected webhook POST: signature validation failed")
            return respond("403 Forbidden", "Invalid signature")
        event = json.loads(raw)
        print(f"Received signed webhook POST: bytes={length}", flush=True)
        forward_inbound_to_whaleads(raw, expected)
        receive(event)
    except (ValueError, TypeError, KeyError):
        logger.exception("Rejected malformed webhook event or missing configuration")
        return respond("400 Bad Request", "Invalid event or configuration")
    except (OSError, sqlite3.Error):
        logger.exception("Webhook processing or WhatsApp reply failed")
        return respond("503 Service Unavailable", "Retry later")
    return respond("200 OK", "OK")
