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



**🧩 Installation Requirements**

**✅ Step 1: Activate Bench Environment**

cd /opt/frappe-bench
source env/bin/activate

**✅ Step 2: Install Required Python Packages**

Install the MQTT and Redis dependencies:

pip install paho-mqtt
pip install redis

**✅ Step 3: Verify Installation**

You can confirm successful installation using:

python -m pip show paho-mqtt


Expected output should show version details such as:

Name: paho-mqtt
Version: 1.6.1

**✅ Step 4: Restart Bench**

After installation, restart your Frappe bench:

bench restart