#!/usr/bin/env python3
"""
Interactive mock AlhamraniServicev2 — a fake mada terminal you drive by hand.

Speaks enough of the ASP.NET SignalR 2.x protocol for jquery.signalR to connect,
so the browser side of the Alhamrani integration can be exercised end to end.

When a transaction arrives, this window prompts you to choose the outcome, the
way you would press keys on a real terminal. That makes every failure path
reachable on demand — including the ones that are nearly impossible to stage
with real hardware, like a wrong-amount echo or a mid-transaction dropout.

    python au_mock_terminal.py --port 9001

Options
    --port N          listen port (default 9000; use 9001 to sit alongside the
                      real service)
    --tid VALUE       TID returned by check2. Set a different value to test the
                      wrong-terminal guard.
    --disconnected    check2 reports is_connected false
    --auto            do not prompt; decide from the last two digits of the
                      amount (00 approve, 51 decline, 99 no reply, 77 wrong
                      amount, 88 wrong ecr_no, 98 code TO, 66 slow)
    --receipt         include a populated receiptData block in approvals

NOT a payment terminal. Never point a production site at this.
"""

import argparse
import json
import queue
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HUB_NAME = "MyHub"          # confirmed from the real service's /signalr/hubs
CALLBACK = "addMessage"     # confirmed from a live response

OPTS = None
CONNECTIONS = {}            # connection token -> Queue of client-bound messages
JOURNAL = {}                # bill_no -> stored response, for GET lookups
LOCK = threading.Lock()

# Transactions waiting on an operator decision.
DECISIONS = queue.Queue()


# --------------------------------------------------------------------------
# the keypad
# --------------------------------------------------------------------------

CHOICES = [
    ("1", "Approve", {"code": "000"}),
    ("2", "Decline - not sufficient funds", {"code": "116"}),
    ("3", "Decline - expired card", {"code": "101"}),
    ("4", "Decline - incorrect PIN", {"code": "117"}),
    ("5", "Decline - do not honour", {"code": "100"}),
    ("6", "Cardholder cancelled at terminal", {"code": "UC"}),
    ("7", "NO RESPONSE AT ALL  (tests timeout / Unconfirmed)", {"silent": True}),
    ("8", "Comms lost, code TO  (tests indeterminate)", {"code": "TO"}),
    ("9", "Approve but echo the WRONG AMOUNT  (tests s9.2 validation)",
     {"code": "000", "bad_amount": True}),
    ("10", "Approve but echo the WRONG ECR NO  (tests s9.2 validation)",
     {"code": "000", "bad_ecr": True}),
    ("11", "Approve after a 30s delay  (tests stale response)",
     {"code": "000", "delay": 30}),
    ("12", "Approve with a DIFFERENT TID  (tests wrong-terminal guard)",
     {"code": "000", "bad_tid": True}),
    ("13", "Enter a custom response code", {"custom": True}),
]

AUTO_MAP = {
    "51": {"code": "116"}, "52": {"code": "101"},
    "99": {"silent": True}, "98": {"code": "TO"},
    "77": {"code": "000", "bad_amount": True},
    "88": {"code": "000", "bad_ecr": True},
    "66": {"code": "000", "delay": 30},
}


def prompt_for(req):
    """Ask the operator what the terminal should do. Blocks this thread."""
    amount = req.get("amount") or "0"
    try:
        display = "%.2f" % (int(amount) / 100.0)
    except (ValueError, TypeError):
        display = amount

    print("\n" + "=" * 66)
    print("  TERMINAL REQUEST   %s" % (req.get("msg_id") or "?"))
    print("  Amount as sent     %s   (= SAR %s if minor units)" % (amount, display))
    print("  Bill no            %s" % (req.get("bill_no") or "-"))
    print("  ECR / receipt no   %s / %s" % (req.get("ecr_no"), req.get("ecr_receipt_no")))
    print("=" * 66)
    for key, label, _ in CHOICES:
        print("   %-3s %s" % (key, label))
    print("-" * 66)

    while True:
        try:
            answer = input("  Choose [1]: ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            print("\n  (no input available, defaulting to Approve)")
            return {"code": "000"}

        for key, label, outcome in CHOICES:
            if answer == key:
                if outcome.get("custom"):
                    code = input("  Response code (e.g. 000, 116, TO): ").strip()
                    return {"code": code or "000"}
                print("  -> %s" % label)
                return dict(outcome)

        print("  Not a valid choice.")


def decision_loop():
    """Runs on the main thread so input() behaves. Serialised on purpose:
    one operator, one terminal, one decision at a time."""
    while True:
        job = DECISIONS.get()
        if job is None:
            return
        req, reply = job
        reply(prompt_for(req))


# --------------------------------------------------------------------------
# building responses
# --------------------------------------------------------------------------

def blank_response(**over):
    """The service returns every field on every reply, mostly null.
    receiptData was observed on a live v1.2.4 service and is not in the PDF."""
    base = {
        "ecr_no": None, "ecr_receipt_no": None, "amount": None, "pan": None,
        "rrn": None, "tid": None, "response_code": None, "auth_code": None,
        "card_type": None, "transaction_date": None, "transaction_time": None,
        "card_expiry_date": None, "is_connected": True, "receiptData": None,
    }
    base.update(over)
    return base


FAKE_RECEIPT = {
    "MERCHANTNAME_EN": "MARINA STORE", "MERCHANTNAME_ARA": "متجر مارينا",
    "ADDRESS_EN": "King Fahd Road", "ADDRESS_ARA": "طريق الملك فهد",
    "CITY_EN": "Riyadh", "CITY_ARA": "الرياض",
    "CARDTYPE_EN": "mada", "CARDTYPE_ARA": "مدى",
    "TXNTYPE_EN": "Purchase", "TXNTYPE_ARA": "شراء",
    "RESP_EN": "APPROVED", "RESP_ARA": "تمت الموافقة",
    "AUTHTEXT_EN": "Approval Code", "AUTHTEXT_ARA": "رمز الموافقة",
    "RETAILER_EN": "Customer's Copy", "RETAILER_ARA": "نسخة العميل",
    "ECR_IDData": "BankID 0001 RetailerID 12345 TID 5517803302112477 MCC 5311",
    "ECR_EMVData": "AID A0000002281010 TVR 0000008000 TSI E800 ACI 00",
    "FTRMADA1_EN": "Thank you", "FTRMADA1_ARA": "شكرا لك",
}


def build_response(req, outcome):
    """Turn an operator choice into the JSON the service would broadcast."""
    if outcome.get("silent"):
        return None, 0

    code = str(outcome.get("code") or "000")
    amount = str(req.get("amount") or "")
    approved = code in ("000", "001", "003", "007", "060", "086", "087", "089",
                        "300", "400", "800")

    t = time.localtime()
    res = blank_response(
        ecr_no=req.get("ecr_no"),
        ecr_receipt_no=req.get("ecr_receipt_no"),
        amount=str(int(amount or 0)).zfill(12) if amount.isdigit() else amount,
        pan="455036******7585",
        rrn=str(int(time.time()))[-12:],
        tid="9999999999999999" if outcome.get("bad_tid") else OPTS.tid,
        response_code=code,
        auth_code=str(uuid.uuid4().int)[:6] if approved else None,
        card_type="MADA",
        transaction_date=time.strftime("%m%d", t),
        transaction_time=time.strftime("%H%M%S", t),
        card_expiry_date="1230",
    )

    if outcome.get("bad_amount") and amount.isdigit():
        res["amount"] = str(int(amount) + 1000).zfill(12)
        print("  [mock] echoing a WRONG amount: %s" % res["amount"])

    if outcome.get("bad_ecr"):
        res["ecr_no"] = "999"
        print("  [mock] echoing a WRONG ecr_no: 999")

    if approved and OPTS.receipt:
        res["receiptData"] = json.dumps(FAKE_RECEIPT, ensure_ascii=False)

    if approved and req.get("bill_no"):
        with LOCK:
            # Matches real behaviour: the first transaction for a bill no wins.
            JOURNAL.setdefault(str(req["bill_no"]), dict(res))

    return res, outcome.get("delay", 1.0)


def handle_get(req):
    """GET by bill number, from the journal."""
    bill = str(req.get("bill_no") or "")
    t = time.localtime()
    with LOCK:
        hit = JOURNAL.get(bill)
    if hit:
        found = dict(hit)
        found["ecr_receipt_no"] = "0001EEEEEE"   # trigger value is echoed back
        print("  [mock] GET %s -> found, code %s" % (bill, found["response_code"]))
        return found, 0.5
    print("  [mock] GET %s -> no record (480)" % bill)
    return blank_response(
        ecr_no=req.get("ecr_no"), ecr_receipt_no="0001EEEEEE",
        response_code="480", transaction_date=time.strftime("%m%d", t),
        transaction_time=time.strftime("%H%M%S", t),
    ), 0.5


def dispatch(keyword, payload, token):
    """Work out the reply and push it to the client after the right delay."""
    try:
        req = json.loads(payload) if isinstance(payload, str) else payload
    except (ValueError, TypeError):
        print("  [mock] unparseable payload: %r" % payload)
        return

    if keyword in ("check", "check2"):
        res = blank_response(is_connected=not OPTS.disconnected)
        if keyword == "check2" and not OPTS.disconnected:
            res["tid"] = OPTS.tid
        print("  [mock] %s -> is_connected=%s tid=%s" % (
            keyword, res["is_connected"], res["tid"]))
        deliver(token, res, 0.3)
        return

    if keyword == "cancel":
        print("  [mock] cancel received, terminal returns to idle")
        return

    msg_id = (req.get("msg_id") or "").upper()

    if msg_id == "GET":
        res, delay = handle_get(req)
        deliver(token, res, delay)
        return

    if msg_id == "REC":
        print("  [mock] reconciliation -> 500 in balance")
        deliver(token, blank_response(
            ecr_no=req.get("ecr_no"), ecr_receipt_no=req.get("ecr_receipt_no"),
            response_code="500", tid=OPTS.tid), 1.0)
        return

    # PUR / REF: ask the operator, unless running unattended.
    if OPTS.auto:
        tail = str(req.get("amount") or "")[-2:]
        outcome = AUTO_MAP.get(tail, {"code": "000"})
        print("  [mock] auto mode, amount ends %s -> %s" % (tail, outcome))
        res, delay = build_response(req, outcome)
        if res is not None:
            deliver(token, res, delay)
        return

    def reply(outcome):
        res, delay = build_response(req, outcome)
        if res is None:
            print("  [mock] sending NO response, client must time out")
            return
        deliver(token, res, delay)

    DECISIONS.put((req, reply))


def deliver(token, res, delay):
    def run():
        if delay:
            time.sleep(delay)
        with LOCK:
            q = CONNECTIONS.get(token)
        if q:
            q.put({"H": HUB_NAME, "M": CALLBACK, "A": ["response", json.dumps(res)]})
        print("  [mock] -> code=%s amount=%s tid=%s" % (
            res.get("response_code"), res.get("amount"), res.get("tid")))
    threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------
# SignalR 2.x transport (long polling)
# --------------------------------------------------------------------------

HUBS_PROXY = """/*! Mock AlhamraniService hub proxy */
(function ($, window) {
    $.hubConnection.prototype.createHubProxies = function () {
        var proxies = {};
        proxies['%(hub)s'] = this.createHubProxy('%(hub)s');
        proxies['%(hub)s'].client = { };
        proxies['%(hub)s'].server = {
            checkDevice: function (ipOrPort) {
                return proxies['%(hub)s'].invoke.apply(proxies['%(hub)s'], $.merge(["CheckDevice"], $.makeArray(arguments)));
            },
            send: function (name, message) {
                return proxies['%(hub)s'].invoke.apply(proxies['%(hub)s'], $.merge(["Send"], $.makeArray(arguments)));
            }
        };
        return proxies;
    };
}(window.jQuery, window));
""" % {"hub": HUB_NAME}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockAlhamraniTerminal/2.0"

    def log_message(self, fmt, *args):
        pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Requested-With")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def reply_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def reply_text(self, text, ctype="text/plain; charset=utf-8"):
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.cors()
        self.end_headers()

    def do_GET(self):
        self._body = ""
        self.route("GET")

    def do_POST(self):
        # Must drain the body here. Leaving it unread desynchronises the
        # keep-alive connection and the next request line gets parsed from the
        # leftover bytes.
        length = int(self.headers.get("Content-Length") or 0)
        self._body = self.rfile.read(length).decode() if length else ""
        self.route("POST")

    def route(self, verb):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)
        token = (qs.get("connectionToken") or [""])[0]

        if path in ("", "/"):
            return self.reply_text(
                "Mock Alhamrani terminal is running.\n"
                "Hub %s at /signalr, callback %s\n" % (HUB_NAME, CALLBACK))

        if path == "/signalr/hubs":
            return self.reply_text(HUBS_PROXY, "application/javascript; charset=utf-8")

        if path == "/signalr/negotiate":
            new_token = str(uuid.uuid4())
            print("[mock] negotiate -> %s" % new_token[:8])
            return self.reply_json({
                "Url": "/signalr",
                "ConnectionToken": new_token,
                "ConnectionId": new_token,
                "KeepAliveTimeout": 20.0,
                "DisconnectTimeout": 30.0,
                "ConnectionTimeout": 110.0,
                "TryWebSockets": False,     # long polling only; see module docstring
                "ProtocolVersion": "2.0",
                "TransportConnectTimeout": 5.0,
                "LongPollDelay": 0.0,
            })

        if path in ("/signalr/connect", "/signalr/reconnect"):
            transport = (qs.get("transport") or [""])[0]
            if transport == "serverSentEvents":
                # Declined so the client falls back to long polling immediately
                # rather than waiting out the connect timeout.
                return self.reply_json({"E": "SSE not supported by mock"}, 400)
            with LOCK:
                CONNECTIONS.setdefault(token, queue.Queue())
            print("[mock] %s transport=%s token=%s" % (
                path.rsplit("/", 1)[-1], transport, token[:8]))
            return self.reply_json({"C": "d-0", "S": 1, "M": []})

        if path == "/signalr/start":
            return self.reply_json({"Response": "started"})

        if path == "/signalr/poll":
            with LOCK:
                q = CONNECTIONS.setdefault(token, queue.Queue())
            messages = []
            try:
                messages.append(q.get(timeout=20))
                while True:
                    messages.append(q.get_nowait())
            except queue.Empty:
                pass
            return self.reply_json({"C": "d-%d" % int(time.time() * 1000), "M": messages})

        if path == "/signalr/send":
            data = parse_qs(self._body).get("data", [""])[0]
            try:
                invocation = json.loads(data)
            except ValueError:
                return self.reply_json({"E": "bad invocation"}, 400)

            if (invocation.get("H") or "").lower() != HUB_NAME.lower():
                return self.reply_json({"I": invocation.get("I"), "E": "no such hub"})

            method = (invocation.get("M") or "").lower()
            args = invocation.get("A") or []
            call_id = invocation.get("I")

            if method == "send":
                dispatch(args[0] if args else "",
                         args[1] if len(args) > 1 else "{}", token)
                return self.reply_json({"I": call_id})

            if method == "checkdevice":
                print("  [mock] CheckDevice(%s) -> %s" % (
                    args[0] if args else "", not OPTS.disconnected))
                return self.reply_json({"I": call_id, "R": not OPTS.disconnected})

            if method == "starttransaction":
                # No bill_no field, so this cannot support GET recovery. The
                # integration uses Send. Present for completeness only.
                dispatch("transaction", {
                    "msg_id": "PUR", "ecr_no": "123",
                    "ecr_receipt_no": "%010d" % (int(time.time()) % 10 ** 10),
                    "amount": str(args[1]) if len(args) > 1 else "",
                    "bill_no": "",
                    "port_no_or_ip_adddress": args[0] if args else "",
                }, token)
                return self.reply_json({"I": call_id})

            return self.reply_json({"I": call_id, "E": "no such method: %s" % method})

        if path == "/signalr/abort":
            with LOCK:
                CONNECTIONS.pop(token, None)
            return self.reply_json({})

        if path == "/signalr/ping":
            return self.reply_json({"Response": "pong"})

        print("[mock] unhandled %s %s" % (verb, path))
        return self.reply_json({"E": "not found: %s" % path}, 404)


def main():
    global OPTS
    parser = argparse.ArgumentParser(description="Interactive mock Alhamrani terminal")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--tid", default="5517803302112477")
    parser.add_argument("--disconnected", action="store_true",
                        help="check2 reports is_connected false")
    parser.add_argument("--auto", action="store_true",
                        help="do not prompt; decide from the amount's last two digits")
    parser.add_argument("--receipt", action="store_true",
                        help="include a populated receiptData block in approvals")
    OPTS = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", OPTS.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("=" * 66)
    print("  Mock Alhamrani terminal on http://localhost:%d/signalr" % OPTS.port)
    print("  hub=%s  callback=%s  tid=%s" % (HUB_NAME, CALLBACK, OPTS.tid))
    print("  mode=%s%s" % ("auto" if OPTS.auto else "interactive",
                           ", receiptData on" if OPTS.receipt else ""))
    print("  NOT a payment terminal. Ctrl-C to stop.")
    print("=" * 66)
    if not OPTS.auto:
        print("\n  Waiting for a transaction. Take a card payment in the POS and\n"
              "  this window will ask you what the terminal should do.\n")

    try:
        decision_loop()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()