import frappe
from frappe.utils import now
import json

@frappe.whitelist(allow_guest=True)
def log_geidea_error(error_message):
    """
    API to log GEIdea errors.
    Supports string, JSON object, or any input.
    """

    try:
        # Convert non-string input to JSON string
        if not isinstance(error_message, str):
            error_message = json.dumps(error_message, ensure_ascii=False)

        # Create new GEIdea Error Log document
        doc = frappe.get_doc({
            "doctype": "GEIdea Error Log",
            "custom_error_message": error_message,
            "custom_time": now(),
            "custom_title": "GEIdea Error"
        })
        
        # Insert the document
        doc.insert(ignore_permissions=True)
        frappe.db.commit()

        return {

            "message": "Error logged successfully",
            "name": doc.name
        }

    except Exception as e:
        frappe.log_error(
            message=frappe.get_traceback(),
            title="GEIdea Error API Failure"
        )
        return {
            "status": "error",
            "message": str(e)
        }
