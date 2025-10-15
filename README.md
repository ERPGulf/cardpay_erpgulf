**🧩 Geidea Payment Integration for Frappe**

This app integrates Frappe with Geidea payment devices using MQTT for real-time communication and Redis for temporary state management.
It allows backend systems to send payment requests to POS terminals and receive asynchronous payment responses securely.

**🚀 Features**

📡 MQTT-based communication between ERPNext/Frappe and Geidea payment terminals.

⚡ Redis caching for fast and temporary message tracking.

🧾 Automatic Frappe logging via the GEIdea Log doctype.

🔒 Secure SSL support for MQTT connections.

⏱️ Auto-expiry handling for stale UUIDs (default: 60 seconds).

✅ Automatic approval/decline detection from device responses.