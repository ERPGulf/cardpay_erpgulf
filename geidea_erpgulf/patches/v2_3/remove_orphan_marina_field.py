# Copyright (c) 2026, ERPGulf and contributors
# For license information, please see license.txt
import frappe


def execute():
    """Remove an orphaned Custom Field that blocks POS Opening Entry.

    custom_marina_payment_terminal is a Link to "Marina Payment Terminal",
    a DocType that no longer exists. Frappe cannot build the meta for POS
    Opening Entry while it is present, so no POS shift can be opened.

    It is system-generated and owned by Administrator, so it cannot be deleted
    from the UI by a normal user. Patches run as Administrator.
    """
    name = "POS Opening Entry-custom_marina_payment_terminal"

    if not frappe.db.exists("Custom Field", name):
        return

    # If the target DocType comes back, the field should be repointed rather
    # than deleted, so leave it alone in that case.
    if frappe.db.exists("DocType", "Marina Payment Terminal"):
        frappe.log_error(
            title="Orphan Marina field patch skipped",
            message="Marina Payment Terminal exists; Custom Field left in place.",
        )
        return

    frappe.delete_doc("Custom Field", name, force=1, ignore_permissions=True)
    frappe.clear_cache(doctype="POS Opening Entry")