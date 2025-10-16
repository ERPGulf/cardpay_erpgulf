import frappe
import json
import ssl
import paho.mqtt.client as mqtt
import time
import redis

# Redis setup
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)

# Redis utility functions
def set_uuid_status(uuid, status):
    redis_client.hset(uuid, "status", status)
    redis_client.hset(uuid, "created_at", str(time.time()))
    redis_client.expire(uuid, 40)  # Auto-expire after 60s

def get_uuid_status(uuid):
    return redis_client.hget(uuid, "status")

def set_uuid_response(uuid, response):
    redis_client.hset(uuid, "response", json.dumps(response))

def get_uuid_response(uuid):
    cached = redis_client.hget(uuid, "response")
    return json.loads(cached) if cached else None

def delete_uuid(uuid):
    redis_client.delete(uuid)
    
def log_geidea(uuid, final_response=None, final_status="Unknown",input_response=None, output_response=None):
    try:
        input_response = redis_client.hget(uuid, "input_response")
        output_response = redis_client.hget(uuid, "output_response")

        final_response = final_response or {"status": "unknown"}
        final_response_str = json.dumps(final_response).lower()

        # Determine Approved/Declined only if no explicit status provided
        if final_status == "Unknown":
            final_status = "Approved" if "approved" in final_response_str else "Declined"

        log_doc = frappe.get_doc({
            "doctype": "GEIdea Log",
            "uuid": uuid,
            "input_response": input_response,
            "output_response": output_response,
            "custom_status_": json.dumps(final_response),
            "custom_status_of_payment": final_status
        })
        log_doc.insert(ignore_permissions=True)
        frappe.db.commit()

    except Exception as e:
        frappe.log_error("GEIdea Log Error", f"UUID: {uuid} | {str(e)}")

@frappe.whitelist(allow_guest=True)
def send_request_to_device():
    try:
        data = json.loads(frappe.request.data)
    except Exception:
        frappe.throw("Invalid JSON input")

    uuid = data.get("uuid")
    if not uuid:
        frappe.throw("UUID is required")

    # frappe.log_error("Send Request", f"Received UUID: {uuid}")
    set_uuid_status(uuid, "pending")

    broadcast_status = send_request_to_device_broadcast(data)
    input_resp = json.dumps(data)
    output_resp = json.dumps(broadcast_status.get("message"))

    # Store input and MQTT payload in Redis
    redis_client.hset(uuid, "input_response", json.dumps(data))
    redis_client.hset(uuid, "output_response", json.dumps(broadcast_status.get("message")))
                                                                 
    if broadcast_status["status"] != "sent":
        return broadcast_status

    geidea_setting = frappe.get_single("MQTT Setting")
    timeout = int(geidea_setting.timespan)
    waited = 0
    # while waited < timeout:
    #     cached = get_uuid_response(uuid)
    #     if cached:
    #         log_geidea(uuid, final_response=cached)
    #         delete_uuid(uuid)
    #         return cached
    while waited < timeout:
        cached = get_uuid_response(uuid)
        if cached:
            # Convert entire response to lowercase string
            response_str = json.dumps(cached).lower()

            # Check if "approved" is present anywhere
            final_status = "Approved" if "approved" in response_str else "Declined"

            # Add numeric status to response (optional)
            status_numeric = 1 if final_status == "Approved" else 0
            cached["final_Status"] = status_numeric

            # Log with clean status
            log_geidea(uuid, final_response=cached, final_status=final_status)

            delete_uuid(uuid)
            return cached
        time.sleep(1)
        waited += 1

    log_geidea(
    uuid,
    final_response={"status": "timeout", "message": "No response from device"},
    final_status="Timeout",
    input_response=input_resp,
    output_response=output_resp
)
    delete_uuid(uuid)
    return {"status": "timeout", "message": "No response from device", "uuid": uuid}

def send_request_to_device_broadcast(data):
    user = data.get("user")
    if not user:
        frappe.throw("User is required")

    device_doc = frappe.get_doc("GEIdea Device Map", {"user": user})
    if not device_doc:
        frappe.throw(f"No device mapping found for user {user}")

    # ✅ Check if device is enabled
    if int(device_doc.custom_device_enabled or 0) != 1:
        return {"status": "disabled", "message": f"Device is not enabled for user {user}"}
    topic = frappe.db.get_value("GEIdea Device Map", {"user": user}, "data")
    if not topic:
        frappe.throw(f"No device mapping found for user {user}")

    geidea_setting = frappe.get_single("MQTT Setting")
    broker_host = geidea_setting.broker_url
    broker_port = int(geidea_setting.port)
    protocol = (geidea_setting.protocol or "").lower()
    mqtt_username = geidea_setting.username
    mqtt_password = geidea_setting.get_password("password")

    payload = dict(data)
    payload["device_topic"] = topic
    message = json.dumps(payload)

    status = "failed"
    error_message = None
    try:
        client = mqtt.Client()
        if mqtt_username and mqtt_password:
            client.username_pw_set(mqtt_username, mqtt_password)
        if protocol == "ssl":
            client.tls_set("/etc/ssl/certs/ca-certificates.crt", tls_version=ssl.PROTOCOL_TLSv1_2)
            client.tls_insecure_set(False)

        client.connect(broker_host, broker_port, 60)
        client.loop_start()
        client.publish(topic, message)
        client.loop_stop()
        client.disconnect()
        status = "sent"
    except Exception as e:
        error_message = str(e)

    return {"status": status, "topic": topic, "message": payload, "error": error_message}


@frappe.whitelist(allow_guest=True)
def device_callback():
    try:
        data = json.loads(frappe.request.data)
    except Exception:
        frappe.throw("Invalid JSON input")

    uuid = data.get("uuid")
    if not uuid:
        frappe.throw("UUID is required in callback")

    status = get_uuid_status(uuid)
    if status != "pending":
        frappe.log_error(
            title=f"Late or invalid callback for UUID {uuid}",
            message=f"Callback received after timeout or for unknown UUID"
        )
        return {"status": "expired", "uuid": uuid, "message": "UUID not found or expired"}

    set_uuid_response(uuid, data)

    # ✅ Detect "approved" anywhere in the response (case-insensitive)
    response_str = json.dumps(data).lower()
    final_status = "Approved" if "approved" in response_str else "Declined"
    input_resp = redis_client.hget(uuid, "input_response")
    output_resp = redis_client.hget(uuid, "output_response")
    
    log_geidea(uuid, final_response=data, final_status=final_status,
    input_response=input_resp,
    output_response=output_resp)

    return {"status": "ok", "uuid": uuid}
