# -*- coding: utf-8 -*-
"""
Alhamrani Universal (AU) mada EFTPOS — server side.

ARCHITECTURAL NOTE — this is NOT shaped like the Geidea integration.

Geidea is server-initiated: posapp.credit_card_payment() POSTs to the geidea app,
which publishes over MQTT and polls Redis until the terminal answers. One
blocking frappe.call from the browser.

Alhamrani cannot work that way. AlhamraniServicev2 runs on the till PC and is
reachable only from that PC's browser (http://localhost:9000/signalr). The Frappe
server can never reach it. So the round trip splits in three:

    begin()   server   reserve the transaction, hand back the request payload
    (browser) SignalR  hub.invoke("send", "transaction", payload)
    finish()  server   validate the echoed response, decide Approved/Declined

Two protocol facts drive the rest of this module:

  1. Responses arrive on a BROADCAST callback (addMessage) with no correlation
     id. We correlate on ecr_receipt_no, unique per request, and revalidate the
     echo server-side because the browser is where the ambiguity lives.

  2. A timeout does NOT mean the card was not charged. Any terminal-level code
     or failed echo check becomes Unconfirmed, and a human must close it out.
     Retrying an Unconfirmed transaction is how a customer gets charged twice.

Reference: AU doc "ECR-POS Integration WEB/HTML JAVASCRIPT" v1.2.4, MI 25-007.
"""

from __future__ import unicode_literals

import json
import time
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime

CARD_MOP = "credit card"          # matches the existing Geidea check in posapp.py
MANAGER_ROLES = ("Accounts Manager", "System Manager")

# --- response codes (AU doc s7.1 / s7.2) ----------------------------------

APPROVED_CODES = {
    "000", "001", "003", "007", "060", "086", "087", "089", "300", "400", "800",
}

# Terminal-level codes: the transaction never reached a decision, or the state
# is unknown. These must NOT be treated as declines.
INDETERMINATE_CODES = {
    "CAN", "NA", "UN", "LC", "CE", "TO", "XC", "UC", "NL", "LB", "NNA", "REJ",
}

CODE_MEANINGS = {
    "000": "Approved", "001": "Honour with identification", "003": "Approved (VIP)",
    "007": "Approved, update ICC", "060": "Approved in Stand-In Processing",
    "086": "No reason to decline", "087": "Offline approved (chip only)",
    "089": "Unable to go online, offline approved", "300": "Successful",
    "400": "Accepted", "500": "Reconciled, in balance", "501": "Reconciled, out of balance",
    "800": "Accepted",
    "100": "Do not honour", "101": "Expired card", "102": "Suspected fraud",
    "104": "Restricted card", "106": "Allowable PIN tries exceeded",
    "107": "Refer to card issuer", "109": "Invalid merchant", "110": "Invalid amount",
    "111": "Invalid card number", "112": "PIN data required", "116": "Not sufficient funds",
    "117": "Incorrect PIN", "118": "No card record", "119": "Not permitted to cardholder",
    "120": "Not permitted to terminal", "121": "Exceeds withdrawal amount limit",
    "122": "Security violation", "125": "Card not effective", "126": "Invalid PIN block",
    "129": "Suspected counterfeit card", "184": "Incorrect CVV",
    "187": "Original transaction not found", "188": "Offline declined",
    "195": "Individual transaction amount exceeds limit",
    "196": "Cumulative contactless limit exceeded",
    "197": "Card differs from original transaction",
    "198": "Refund amount exceeds original", "199": "Outside mada time limit",
    "208": "Lost card", "209": "Stolen card", "480": "Original transaction not found",
    "481": "Original transaction found but declined", "888": "Unknown error",
    "902": "Invalid transaction", "903": "Re-enter transaction", "909": "System malfunction",
    "913": "Duplicate transmission", "940": "Unknown terminal",
    "CAN": "Timeout / cancelled by user", "NA": "No answer", "LC": "Lost communication",
    "CE": "Comms error", "TO": "Timeout", "UC": "User cancelled", "NL": "No line",
    "LB": "Line busy", "NNA": "Not available", "REJ": "Rejected",
}


def _code(value):
    return str(value or "").strip().upper()


def describe(code):
    return CODE_MEANINGS.get(_code(code), "Unknown code {0}".format(_code(code)))


# --- lookups --------------------------------------------------------------

def _settings():
    return frappe.get_cached_doc("Alhamrani Payment Settings")


def _device(user=None):
    """Resolve the terminal from the till login, same pattern as GEIdea Device Map."""
    user = user or frappe.session.user
    name = frappe.db.get_value(
        "Alhamrani Device Map", {"user": user, "device_enabled": 1}, "name"
    )
    if not name:
        frappe.throw(
            _("No enabled Alhamrani Device Map for {0}. Card payments are unavailable at this till.").format(user),
            title=_("Terminal not configured"),
        )
    return frappe.get_cached_doc("Alhamrani Device Map", name)


@frappe.whitelist()
def is_device_enabled():
    """Mirror of posapp.is_device_enabled() for Geidea."""
    return cint(
        frappe.db.get_value(
            "Alhamrani Device Map", {"user": frappe.session.user}, "device_enabled"
        )
        or 0
    )


@frappe.whitelist()
def get_card_provider():
    """Which terminal this till uses. A user is one or the other, never both.

    Called once on POS open so the Vue layer branches in a single place instead
    of checking two booleans at every payment step.
    """
    user = frappe.session.user

    if cint(frappe.db.get_value("Alhamrani Device Map", {"user": user}, "device_enabled")):
        return "alhamrani"

    if cint(frappe.db.get_value("GEIdea Device Map", {"user": user}, "custom_device_enabled")):
        return "geidea"

    return None


# --- formatting -----------------------------------------------------------

def format_amount(amount, settings=None):
    """AU's own documents disagree, so this is configuration, not a constant.

    doc v1.2.4 s10  -> 000000000100 for SAR 1.00  (minor units)
    MI 25-007       -> 000000000001 for "1.00 SAR" (major units)

    Verify with a SAR 3.47 test charge before go-live. Wrong setting = 100x error.

    Deliberately computed from the numeric value. Do NOT copy the vendor
    sample's val().replace(".", "") — string replace only substitutes the first
    occurrence, and an input with no decimal point passes through unchanged, so
    "1000" meaning SAR 1000 would be sent as SAR 10.00.
    """
    settings = settings or _settings()
    multiplier = cint(settings.amount_multiplier) or 100
    magnitude = abs(flt(amount))
    value = int(round(magnitude * multiplier))

    if value <= 0:
        frappe.throw(_("Amount must be greater than zero."))

    text = str(value)
    if settings.zero_pad_amount:
        text = text.zfill(cint(settings.amount_pad_length) or 12)
    if len(text) > 12:
        frappe.throw(_("Amount {0} exceeds the 12-digit AU field limit.").format(text))
    return text


def _next_bill_no(attempt=1):
    """Numeric, unique, <= 19 chars (AU spec: N 19).

    Sales Invoice names contain letters and can exceed 19 chars, so they cannot
    be used. Epoch ms + 2-digit attempt = 15 digits.

    A fresh number per attempt is mandatory: per MI 25-007, if the same bill
    number is reused the terminal returns only the FIRST transaction, so a retry
    can hide an approved second attempt.
    """
    return "{0}{1:02d}".format(int(time.time() * 1000), min(cint(attempt) or 1, 99))


def _next_receipt_no():
    """N 10. The only correlation key the protocol provides."""
    return str(int(time.time() * 1000) % 10000000000).zfill(10)


# --- session --------------------------------------------------------------

@frappe.whitelist()
def get_config(pos_profile=None):
    """Everything the browser needs on POS open.

    pos_profile is accepted for parity with alhamrani_payment.init(pos_profile)
    on the client. The device is resolved from frappe.session.user regardless,
    so it is currently unused here.
    """
    settings = _settings()
    device = _device()
    return {
        "hub_url": settings.hub_url,
        "hub_name": settings.hub_name,
        "callback_name": settings.callback_name,
        "purchase_timeout_ms": (cint(settings.purchase_timeout_seconds) or 180) * 1000,
        "query_timeout_ms": (cint(settings.query_timeout_seconds) or 20) * 1000,
        "device": {
            "name": device.name,
            "connection": device.connection,
            "address": device.terminal_address,
            "ecr_no": device.ecr_no,
            "expected_tid": device.expected_tid,
            "supports_bill_get": cint(device.supports_bill_get),
            "print_format": device.print_reciept_configuration,
        },
        "unconfirmed": get_unconfirmed(),
    }


@frappe.whitelist()
def record_tid(tid):
    """Result of check2 on POS open.

    On Wi-Fi every till PC can reach every terminal on the subnet, so a stale
    address does not fail — it succeeds against someone else's terminal. This is
    the only guard against charging a customer standing at another till.
    """
    device = _device()
    settings = _settings()
    tid = (tid or "").strip()

    if cint(settings.require_tid_match) and device.expected_tid and tid and tid != device.expected_tid:
        frappe.throw(
            _("Wrong terminal. This till expects TID {0}, but the terminal at {1} reports {2}. "
              "Correct the Terminal Address before taking any payment.").format(
                device.expected_tid, device.terminal_address, tid),
            title=_("Terminal mismatch"),
        )

    adopted = False
    if tid:
        if not device.expected_tid:
            frappe.db.set_value("Alhamrani Device Map", device.name, "expected_tid", tid,
                                update_modified=False)
            adopted = True
        frappe.db.set_value("Alhamrani Device Map", device.name, {
            "last_seen_tid": tid,
            "last_seen_on": now_datetime(),
        }, update_modified=False)
        frappe.db.commit()

    return {"tid": tid, "adopted": adopted}


# --- transaction lifecycle ------------------------------------------------

@frappe.whitelist()
def begin(amount, pos_invoice=None, pos_profile=None, msg_id="PUR", attempt=1):
    """Reserve the transaction BEFORE anything is sent to the terminal.

    Written first so that a browser crash between send and response still leaves
    a record to reconcile against. Returns the exact payload for the browser to
    hand to hub.invoke("send", ...).

    pos_invoice / pos_profile match alhamrani_payment.purchase() and
    .reconcile() on the client exactly (see DEVELOPER_GUIDE.md s6). REC has no
    invoice, so pos_invoice is optional; pos_profile is derived from the
    invoice when one is given and not already supplied.
    """
    settings = _settings()
    device = _device()
    amount_sent = format_amount(amount, settings) if flt(amount) else ""

    invoice_name = pos_invoice
    if invoice_name and not pos_profile:
        pos_profile = frappe.db.get_value("Sales Invoice", invoice_name, "pos_profile")

    doc = frappe.get_doc({
        "doctype": "Alhamrani Transaction",
        "status": "Pending",
        "device_map": device.name,
        "pos_profile": pos_profile,
        "sales_invoice": invoice_name,
        "msg_id": msg_id,
        "attempt": cint(attempt) or 1,
        "bill_no": _next_bill_no(attempt),
        "ecr_no_sent": device.ecr_no,
        "ecr_receipt_no_sent": _next_receipt_no(),
        "amount": flt(amount),
        "amount_sent": amount_sent,
        "sent_at": now_datetime(),
    })

    request = {
        "msg_id": msg_id,
        "ecr_no": device.ecr_no,
        "ecr_receipt_no": doc.ecr_receipt_no_sent,
        "amount": amount_sent,
        "field1": "", "field2": "", "field3": "", "field4": "", "field5": "",
        # Vendor spelling, three d's. Do not correct it.
        "port_no_or_ip_adddress": device.terminal_address,
        "bill_no": doc.bill_no,
    }

    # Refund needs the original RRN + date + masked PAN, exactly as the Geidea
    # refund path pulls custom_transaction_id and posting_date from the original.
    if msg_id == "REF":
        rrn, date_ddmmyyyy, pan = _original_card_details(invoice_name)
        request["field2"] = "{0}{1}".format(rrn, date_ddmmyyyy)
        request["field3"] = pan or ""

    doc.request_json = json.dumps(request, indent=2)
    doc.insert(ignore_permissions=True)
    frappe.db.commit()

    return {"txn": doc.name, "request": request}


def _original_card_details(invoice_name):
    """RRN, DDMMYYYY and masked PAN of the transaction being refunded."""
    invoice = frappe.get_doc("Sales Invoice", invoice_name)
    original_name = invoice.get("return_against") or invoice_name
    original = frappe.get_doc("Sales Invoice", original_name)

    rrn = None
    for payment in original.payments:
        if payment.mode_of_payment and payment.mode_of_payment.lower() == CARD_MOP:
            if payment.get("custom_transaction_id"):
                rrn = payment.custom_transaction_id
                break

    pan = None
    approved = frappe.get_all(
        "Alhamrani Transaction",
        filters={"sales_invoice": original_name, "status": "Approved", "msg_id": "PUR"},
        fields=["rrn", "pan"],
        limit=1,
    )
    if approved:
        rrn = rrn or approved[0].rrn
        pan = approved[0].pan

    if not rrn:
        frappe.throw(
            _("Cannot refund: no card transaction reference found on {0}.").format(original_name)
        )

    posting = datetime.strptime(str(original.posting_date), "%Y-%m-%d").strftime("%d%m%Y")
    return rrn, posting, pan


@frappe.whitelist()
def finish(txn, response):
    """Validate the echoed response and record the decision."""
    if isinstance(response, str):
        response = json.loads(response)

    doc = frappe.get_doc("Alhamrani Transaction", txn)

    if doc.status != "Pending":
        # Late or duplicate broadcast. Report what was already decided.
        return {
            "status": doc.status,
            "approved": doc.status == "Approved",
            "code": doc.response_code,
            "meaning": doc.response_meaning,
            "rrn": doc.rrn,
            "already_recorded": True,
        }

    settings = _settings()
    doc.response_json = json.dumps(response, indent=2)
    doc.responded_at = now_datetime()
    doc.response_age_seconds = int(
        (get_datetime(doc.responded_at) - get_datetime(doc.sent_at)).total_seconds()
    )

    code = _code(response.get("response_code"))
    doc.response_code = code
    doc.response_meaning = describe(code)
    doc.auth_code = response.get("auth_code")
    doc.rrn = response.get("rrn")
    doc.tid = response.get("tid")
    doc.card_type = response.get("card_type")
    doc.pan = response.get("pan")
    doc.card_expiry_date = response.get("card_expiry_date")
    doc.amount_echoed = response.get("amount")

    problems = _validate_echo(doc, response, settings)

    if problems:
        # A failed echo check means this response may belong to a different
        # transaction. Unknown, never approved.
        doc.status = "Unconfirmed"
        doc.resolution_note = _("Validation failed: {0}").format("; ".join(problems))
    elif code in APPROVED_CODES:
        doc.status = "Approved"
    elif code in INDETERMINATE_CODES:
        doc.status = "Unconfirmed"
    else:
        doc.status = "Declined"

    doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "status": doc.status,
        "approved": doc.status == "Approved",
        "code": code,
        "meaning": doc.response_meaning,
        "problems": problems,
        "rrn": doc.rrn,
        "auth_code": doc.auth_code,
        "tid": doc.tid,
        "card_type": doc.card_type,
        "pan": doc.pan,
        "bill_no": doc.bill_no,
        "txn": doc.name,
    }


def _validate_echo(doc, response, settings):
    """AU doc s9.2 mandates these three checks plus the age limit."""
    problems = []

    echoed_ecr = str(response.get("ecr_no") or "").strip()
    if echoed_ecr and echoed_ecr != str(doc.ecr_no_sent):
        problems.append(_("ECR no mismatch: sent {0}, received {1}").format(doc.ecr_no_sent, echoed_ecr))

    echoed_receipt = str(response.get("ecr_receipt_no") or "").strip()
    if echoed_receipt and echoed_receipt != str(doc.ecr_receipt_no_sent):
        problems.append(_("Receipt no mismatch: sent {0}, received {1}").format(
            doc.ecr_receipt_no_sent, echoed_receipt))

    echoed_amount = str(response.get("amount") or "").strip()
    if doc.msg_id in ("PUR", "REF") and echoed_amount and doc.amount_sent:
        try:
            # Compare numerically: the terminal zero-pads, we may not.
            if int(echoed_amount) != int(doc.amount_sent):
                problems.append(_("Amount mismatch: sent {0}, received {1}").format(
                    doc.amount_sent, echoed_amount))
        except ValueError:
            problems.append(_("Amount echoed is not numeric: {0}").format(echoed_amount))

    max_age = cint(settings.response_max_age_seconds) or 900
    if cint(doc.response_age_seconds) > max_age:
        problems.append(_("Response arrived after {0}s, limit is {1}s").format(
            doc.response_age_seconds, max_age))

    return problems


@frappe.whitelist()
def mark_unconfirmed(txn, reason=None):
    """Called on timeout or on SignalR disconnect."""
    doc = frappe.get_doc("Alhamrani Transaction", txn)
    if doc.status == "Pending":
        doc.status = "Unconfirmed"
        doc.resolution_note = reason or _("No response received within the timeout.")
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    return {"status": doc.status}


@frappe.whitelist()
def get_unconfirmed():
    """Anything the cashier must close out before selling again."""
    device = frappe.db.get_value(
        "Alhamrani Device Map", {"user": frappe.session.user, "device_enabled": 1}, "name"
    )
    if not device:
        return []
    return frappe.get_all(
        "Alhamrani Transaction",
        filters={"device_map": device, "status": ("in", ["Pending", "Unconfirmed"])},
        fields=["name", "status", "bill_no", "amount", "sales_invoice", "attempt",
                "sent_at", "response_code", "response_meaning"],
        order_by="sent_at asc",
    )


@frappe.whitelist()
def resolve(txn, resolution, note=None):
    """Record what actually happened to an unconfirmed payment."""
    if not note:
        frappe.throw(_("Record how you confirmed the outcome: terminal display, printed receipt, or bank statement."))

    doc = frappe.get_doc("Alhamrani Transaction", txn)
    if doc.status not in ("Unconfirmed", "Pending"):
        frappe.throw(_("Transaction {0} is {1} and does not need resolving.").format(txn, doc.status))

    if resolution == "Written Off" and not any(frappe.has_role(r) for r in MANAGER_ROLES):
        frappe.throw(_("Only an Accounts Manager may write off a payment."))

    doc.resolution = resolution
    doc.resolution_note = note
    doc.status = "Resolved"
    doc.resolved_by = frappe.session.user
    doc.resolved_on = now_datetime()
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"status": doc.status}


# --- submit guard ---------------------------------------------------------

def stamp_and_guard(invoice_doc):
    if not is_device_enabled():
        return

    is_return = bool(invoice_doc.get("is_return"))
    card_total = 0.0
    card_rows = []
    for payment in (invoice_doc.payments or []):
        if payment.mode_of_payment and payment.mode_of_payment.lower() == CARD_MOP:
            amt = flt(payment.amount)
            if (is_return and amt < 0) or (not is_return and amt > 0):
                card_total += abs(amt)
                card_rows.append(payment)

    if card_total <= 0:
        return  # cash only

    approved = frappe.get_all(
        "Alhamrani Transaction",
        filters={
            "sales_invoice": invoice_doc.name,
            "status": "Approved",
            "msg_id": "REF" if is_return else "PUR",
        },
        fields=["name", "amount", "rrn", "auth_code", "tid", "pan", "card_type"],
        order_by="creation desc",
    )

    if not _settings().block_submit_without_approval:
        approved and _write_reference(invoice_doc, card_rows, approved[0])
        return

    if not approved:
        frappe.throw(
            _("This invoice has a card payment of {0} but no approved terminal transaction. "
              "Complete the payment at the terminal, or resolve the outstanding "
              "Alhamrani Transaction first.").format(
                frappe.format_value(card_total, {"fieldtype": "Currency",
                                                 "options": invoice_doc.currency})),
            title=_("Card payment not approved"),
        )

    approved_total = sum(flt(row.amount) for row in approved)
    if approved_total + 0.005 < card_total:
        frappe.throw(
            _("Approved card amount {0} is less than the card payment of {1}.").format(
                approved_total, card_total),
            title=_("Amount mismatch"),
        )

    _write_reference(invoice_doc, card_rows, approved[0])


def _write_reference(invoice_doc, card_rows, txn):
    """RRN goes in custom_transaction_id, same field Geidea uses."""
    for payment in card_rows:
        if not payment.get("custom_transaction_id"):
            payment.custom_transaction_id = txn.rrn

    invoice_doc.db_set("custom_alhamrani_transaction", txn.name, update_modified=False)