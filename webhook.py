"""WhatsApp Cloud API adapter. Serve app behind an HTTPS WSGI host."""
import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import closing
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

from bot import OrderChat


def process(db, message):
    message_id = message["id"]
    customer = message["from"]
    with db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT reply, sent FROM events WHERE id = ?", (message_id,)
        ).fetchone()
        if previous:
            return previous
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
        state = {key: value for key, value in vars(chat).items()
                 if key not in {"database", "customer"}}
        state["cart"] = [[name, price, qty] for (name, price), qty in chat.cart.items()]
        db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (customer, json.dumps(state)))
        db.execute("INSERT INTO events VALUES (?, ?, 0)", (message_id, reply))
    return reply, False


def send_reply(phone_id, recipient, reply):
    version = os.environ["WA_GRAPH_VERSION"]
    body = dict(messaging_product="whatsapp", to=recipient, type="text", text={"body": reply})
    request = Request(
        f"https://graph.facebook.com/{version}/{phone_id}/messages",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + os.environ["WA_ACCESS_TOKEN"],
                 "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=8):
        pass


def receive(event):
    phone_id = os.environ["WA_PHONE_NUMBER_ID"]
    with closing(sqlite3.connect(os.environ["WA_DATABASE"])) as db:
        db.execute("CREATE TABLE IF NOT EXISTS sessions (customer TEXT PRIMARY KEY, state TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, reply TEXT, sent INTEGER)")
        for entry in event.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if value.get("metadata", {}).get("phone_number_id") != phone_id:
                    continue
                for message in value.get("messages", []):
                    if message.get("type") != "text":
                        continue
                    reply, sent = process(db, message)
                    if not sent:
                        send_reply(phone_id, message["from"], reply)
                        with db:
                            db.execute("UPDATE events SET sent = 1 WHERE id = ?", (message["id"],))


def app(environ, start_response):
    def respond(status, body):
        start_response(status, [("Content-Type", "text/plain; charset=utf-8")])
        return [body.encode()]

    if environ["PATH_INFO"] == "/health" and environ["REQUEST_METHOD"] == "GET":
        return respond("200 OK", "OK")
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
            return respond("403 Forbidden", "Invalid signature")
        receive(json.loads(raw))
    except (ValueError, TypeError, KeyError):
        return respond("400 Bad Request", "Invalid event or configuration")
    except (OSError, sqlite3.Error):
        return respond("503 Service Unavailable", "Retry later")
    return respond("200 OK", "OK")
