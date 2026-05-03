# GEIDEA – ERPNext Credit Card Payment Integration

> Seamless payment processing between **ERPNext / POS Awesome** and **GEIDEA** credit card terminals via MQTT.

---

## Overview

This project bridges ERPNext (and its POS Awesome module) with GEIDEA credit card machines. When a sale is made, the transaction amount is pushed to the physical credit card terminal over MQTT. The terminal processes the payment and sends the result back directly to the ERPNext server via a REST callback API.

**Repositories**

| Component | Repository | Technology |
|-----------|-----------|------------|
| Backend (ERPNext App) | [cardpay_erpgulf](https://github.com/ERPGulf/cardpay_erpgulf) | Python / Frappe Framework |
| Frontend (Android App) | [geidea-claudion](https://github.com/ERPGulf/geidea-claudion) | Kotlin / Android |

---

## Architecture

![Architecture Diagram](architecture_diagram.png)

### Flow Summary

**Step 1 – POS Awesome initiates payment**

The cashier triggers a card payment from POS Awesome, sending transaction details to the Frappe server:

```json
{
  "user": "husna123",
  "amount": 1250.00,
  "invoice_number": "INV-001",
  "customer": "John Doe"
}
```

**Step 2 – Frappe server maps user to device**

The Frappe backend looks up the `GEIdea Device Map` to find the terminal assigned to that user, resolves the device ID and MQTT broker URL, then publishes the payload to the MQTT broker:

```json
{
  "device_id": "CCM-987654",
  "amount": 1250.00,
  "customer": "John Doe",
  "invoice_number": "INV-001",
  "broker_url": "mqtt://broker.example.com:1883"
}
```

**Step 3 – MQTT broker routes message to terminal**

The credit card machine subscribes to its dedicated MQTT topic. The broker delivers the payment request, including invoice number and amount, directly to the terminal.

**Step 4 – Terminal processes payment & publishes result**

The GEIDEA terminal (running the geidea-claudion Android app) processes the card transaction and publishes the result back to the MQTT broker:

```json
{
  "invoice_number": "INV-20250822-001",
  "status": "approved",
  "transaction_id": "TXN-78"
}
```

**Step 5 – Frappe receives result & responds to POS Awesome**

The Frappe server picks up the status from the broker, logs the transaction in `GEIdea Log`, and returns the final result to POS Awesome:

```json
{
  "invoice_number": "INV-20250822-001",
  "status": "approved",
  "transaction_id": "TXN-78"
}
```

---

## Components

### 1. Backend – `cardpay_erpgulf` (Frappe App)

#### Key Doctypes

| Doctype | Purpose |
|---------|---------|
| `MQTT Setting` | Global MQTT broker configuration (host, port, protocol, credentials, timeout) |
| `GEIdea Device Map` | Maps an ERPNext user to a specific terminal MQTT topic |
| `GEIdea Log` | Audit log of every transaction (input, output, status) |
| `CardPay Settings` | Provider settings (GEIDEA), connection type (IP), merchant credentials |

#### Key API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/method/.../send_request_to_device` | Sends payment request from ERPNext to terminal via MQTT |
| `POST` | `/api/method/.../device_callback` | Receives payment result from the Android terminal |

#### `send_request_to_device` – Request Payload (POS Awesome → Frappe)

```json
{
  "uuid": "unique-transaction-id",
  "user": "cashier@example.com",
  "amount": 100.00,
  "invoice_number": "INV-001",
  "customer": "John Doe",
  "refund": 0,
  "transaction_id": "",
  "posting_date": ""
}
```

> When `refund` is `1`, both `transaction_id` and `posting_date` are **required**.

#### MQTT Payload (Frappe → MQTT Broker → Terminal)

The Frappe backend forwards the entire request above to the MQTT broker, and appends two additional fields resolved from the **GEIdea Device Map**:

```json
{
  "uuid": "unique-transaction-id",
  "user": "cashier@example.com",
  "amount": 100.00,
  "invoice_number": "INV-001",
  "customer": "John Doe",
  "refund": 0,
  "transaction_id": "",
  "posting_date": "",
  "device_topic": "5b548bcb1371d319",
  "custom_print_reciept_configuration": 0
}
```

| Field | Source | Description |
|-------|--------|-------------|
| `uuid` | POS Awesome | Unique transaction identifier |
| `user` | POS Awesome | ERPNext cashier username |
| `amount` | POS Awesome | Transaction amount |
| `invoice_number` | POS Awesome | Sales invoice reference |
| `customer` | POS Awesome | Customer name |
| `refund` | POS Awesome | `0` = payment, `1` = refund |
| `transaction_id` | POS Awesome | Original transaction ID (required when `refund=1`) |
| `posting_date` | POS Awesome | Posting date (required when `refund=1`) |
| `device_topic` | Frappe (Device Map) | Android ID of the terminal — also the MQTT topic |
| `custom_print_reciept_configuration` | Frappe (Device Map) | Receipt print config as integer (e.g. `0` = no print) |

This full payload is published to the topic matching the terminal's Android ID. The message is also stored in Redis (keyed by `uuid`) for audit logging.

#### State Management (Redis)

Redis is used to manage in-flight transaction state and prevent duplicate requests:

| Key Pattern | Purpose |
|-------------|---------|
| `{uuid}` → `status`, `response`, `input_response`, `output_response` | Per-transaction state (TTL: 60 s) |
| `device_active:{topic}` | Device busy lock – prevents concurrent requests to same terminal (TTL: 60 s) |

The `send_request_to_device` API polls Redis every second, up to the configured **Time-Span** (default: 60 s), waiting for the callback to populate a response.

#### MQTT Configuration (MQTT Setting Doctype)

| Field | Example Value |
|-------|--------------|
| Protocol | `ssl` |
| Broker Host | `xxx.claudion.com` |
| Port | `8883` |
| Username | `dK7pX3aN9tB1qL5mR8zF` |
| Password | _(stored securely)_ |
| Time-Span | `60` (seconds) |

---

### 2. Frontend – `geidea-claudion` (Android App)

<img src="app_screenshot.png" alt="GEIDEA-Claudion App" width="260" align="right" style="margin-left:20px"/>

An Android application installed on the GEIDEA credit card terminal. It:

- Connects to the MQTT broker using credentials entered in its built-in settings page.
- Subscribes to its own MQTT topic (derived from the device's Android ID) on startup.
- Receives payment request payloads from ERPNext.
- Initiates the payment flow on the GEIDEA terminal hardware.
- Posts the transaction result back to ERPNext via HTTP.

#### How the App Selects Its MQTT Server

The MQTT broker is **not hardcoded**. After the app is installed from the GEIDEA App Store, an administrator opens the in-app **Settings page** and enters the broker details manually:

| Setting | Description |
|---------|-------------|
| Broker Host | MQTT server hostname (e.g. `xxx.claudion.com`) |
| Port | Typically `8883` for SSL |
| Username | Provided by Claudion team |
| Password | Provided by Claudion team |
| Callback URL | ERPNext server URL for posting payment results back |

These credentials are saved on the device and used every time the app starts to establish the MQTT connection.

> **Important:** The MQTT broker details are provided by the Claudion team. Contact [support@erpgulf.com](mailto:support@erpgulf.com) to obtain them. Only KSA-hosted, SSL-secured brokers are supported.

#### How the App Knows Which MQTT Topic to Subscribe To

The app subscribes to a topic based on its own **Android ID** — a unique identifier the device already knows without any external input. This is displayed on the app's main screen (e.g. `5b548bcb1371d319`).

On the ERPNext side, this same Android ID is entered into the **Device Topic** field of the **GEIdea Device Map** doctype and linked to the cashier user who operates that terminal. This is the one-time manual setup step that ties a physical machine to an ERPNext user.

![GEIdea Device Map](device_map_screenshot.png)

The map record has three key fields:

| Field | Description |
|-------|-------------|
| **Device Enabled** | Must be checked for the device to receive requests |
| **User** | ERPNext cashier account (e.g. `vpsahana2@gmail.com`) |
| **Device Topic** | The Android ID of the terminal (e.g. `5b548bcb1371d319`) — this becomes the MQTT topic |
| **Print Receipt Configuration** | Controls receipt printing behaviour on the terminal |

In the backend code, this is resolved as:

```python
topic = device_doc.data   # Device Topic field = Android ID = MQTT topic
```

So when ERPNext receives `"user": "husna123"` from POS Awesome, it queries the device map, retrieves the Android ID as the topic, and publishes the payment payload to that topic on the broker. The terminal — already subscribed to its own Android ID — receives it instantly.

```
Cashier user       ──►  GEIdea Device Map  ──►  Android ID   ==  MQTT Topic
(e.g. husna123)         (ERPNext lookup)         (e.g. 5b548bcb1371d319)
```

#### Callback API Call (Android → ERPNext)

```
POST {callback_url}/api/method/geidea_erpgulf.geidea_erpgulf.posaw_test.device_callback
Content-Type: application/json

{
  "uuid": "unique-transaction-id",
  "responseCode": "00",
  "responseMessage": "Approved",
  ...
}
```

The app uses **OkHttp** for HTTP and **Paho MQTT** (or equivalent) for broker communication.

---

## Installation

### Backend (ERPNext App)

```bash
# From your Frappe bench directory
bench get-app https://github.com/ERPGulf/cardpay_erpgulf
bench --site your-site.com install-app cardpay_erpgulf
bench --site your-site.com migrate
```

**Dependencies:**
- `paho-mqtt` – MQTT client
- `redis` – State management

```bash
pip install paho-mqtt redis
```

### Android App

The Android app is **not available on the Google Play Store or Apple App Store**, and cannot be side-loaded manually onto the terminal.

It is distributed exclusively through the **GEIDEA App Store**, which is available on GEIDEA-provided terminals only. To get the app installed on your terminal, contact your GEIDEA account representative or reach out to [support@erpgulf.com](mailto:support@erpgulf.com).

---

## Configuration

### Step 1 – MQTT Setting (ERPNext)

Navigate to **MQTT Setting** in ERPNext and fill in:

- Protocol: `ssl`
- Broker Host: your MQTT broker hostname (provided by Claudion — see below)
- Port: `8883` (SSL)
- Username / Password: broker credentials (provided by Claudion)
- Time-Span: polling timeout in seconds (e.g., `60`)

> **Important:** The MQTT server is provided by the Claudion team. Contact [support@erpgulf.com](mailto:support@erpgulf.com) to request access. Only KSA-hosted, SSL-secured servers are supported — do not use unverified or non-KSA servers.

### Step 2 – GEIdea Device Map (ERPNext)

Create a record mapping each cashier user to their terminal's MQTT topic:

| Field | Description |
|-------|-------------|
| User | ERPNext user (cashier) |
| Data (Topic) | MQTT topic for this terminal |
| Device Enabled | Must be checked (`1`) for requests to be sent |
| Print Receipt Configuration | Receipt format (e.g., `0 - No Print`) |

### Step 3 – CardPay Settings (ERPNext)

Navigate to **CardPay Settings → New** and configure:

- Provider: `GEIDEA`
- Connection Type: `IP`
- Machine IP, Merchant ID, API Key, Secret Key

### Step 4 – Android App

Configure the app with:
- MQTT broker credentials (matching MQTT Setting above)
- ERPNext server callback URL

---

## Transaction Status

| Status | Meaning |
|--------|---------|
| `Approved` | Payment successful |
| `Declined` | Payment rejected by the terminal or bank |
| `Timeout` | No response from device within the configured Time-Span |
| `pending_request` | Another transaction is already in progress on this device |
| `disabled` | Device is not enabled in GEIdea Device Map |
| `user_not_registered_for_device_mapping` | No device mapping found for the given user |

---

## Logging

Every transaction is logged in the **GEIdea Log** doctype with:

- `uuid` – Unique transaction identifier
- `input_response` – Original request payload
- `output_response` – MQTT publish payload
- `custom_status_` – Full response JSON
- `custom_status_of_payment` – `Approved` / `Declined` / `Timeout`

---

## Security Notes

- The MQTT broker uses **SSL/TLS** (port 8883) with certificate verification.
- ERPNext stores the MQTT password using Frappe's encrypted password field (`get_password`).
- The `device_callback` endpoint is open (`allow_guest=True`) since terminals do not have ERPNext sessions; validate by UUID and short TTL to mitigate abuse.
- Redis keys auto-expire after **60 seconds** to prevent stale state.

---


## Supported Payment Cards

Accepted card schemes are determined by each merchant's individual arrangement with GEIDEA. Generally, **MADA cards** are accepted by default. Support for **Visa**, **Mastercard**, and other international schemes depends on your GEIDEA account setup. Contact your GEIDEA account manager for details.

---

## License & Copyright

This software is **proprietary and copyright protected**. It is not open-source.

© ERPGulf & Claudion. All rights reserved. Unauthorized copying, distribution, or modification is strictly prohibited.

For licensing inquiries, visit [erpgulf.com](https://erpgulf.com) or [claudion.com](https://claudion.com).

---

## Contact

| | |
|---|---|
| **Support** | [support@erpgulf.com](mailto:support@erpgulf.com) |
| **Sales** | [sales@erpgulf.com](mailto:sales@erpgulf.com) |
| **Website** | [erpgulf.com](https://erpgulf.com) · [claudion.com](https://claudion.com) |