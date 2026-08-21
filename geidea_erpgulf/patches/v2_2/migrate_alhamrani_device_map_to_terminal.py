# Copyright (c) 2026, ERPGulf and contributors
# For license information, please see license.txt

import frappe


def execute():
    """Migrate user-bound 'Alhamrani Device Map' rows into user-less
    'Alhamrani Terminal' rows. POS Profile <-> terminal assignment is NOT
    inferred automatically -- the old schema had no POS Profile field to
    infer it from -- so this patch leaves that step for an admin to do via
    the POS Profile UI after reviewing the migrated terminals.
    """
    if not frappe.db.table_exists("Alhamrani Device Map"):
        return

    if not frappe.db.table_exists("Alhamrani Terminal"):
        frappe.reload_doctype("Alhamrani Terminal")
        frappe.reload_doctype("POS Profile Alhamrani Terminal")

    old_rows = frappe.get_all(
        "Alhamrani Device Map",
        fields=[
            "name", "user", "device_enabled", "connection", "terminal_address",
            "ecr_no", "expected_tid", "last_seen_tid", "last_seen_on",
            "terminal_model", "supports_bill_get", "print_reciept_configuration",
        ],
    )

    created = []
    for row in old_rows:
        # Derive a readable terminal reference from the old user + address,
        # since the old schema had nothing better to key on.
        terminal_id = "{0}-{1}".format(
            (row.user or "TERMINAL").split("@")[0].upper(),
            (row.terminal_address or "").replace(".", "").replace(":", "") or "T",
        )

        if frappe.db.exists("Alhamrani Terminal", terminal_id):
            terminal_id = "{0}-{1}".format(terminal_id, row.name[:6])

        doc = frappe.get_doc({
            "doctype": "Alhamrani Terminal",
            "terminal_id": terminal_id,
            "device_enabled": row.device_enabled,
            "connection": row.connection,
            "terminal_address": row.terminal_address,
            "ecr_no": row.ecr_no,
            "expected_tid": row.expected_tid,
            "last_seen_tid": row.last_seen_tid,
            "last_seen_on": row.last_seen_on,
            "terminal_model": row.terminal_model,
            "supports_bill_get": row.supports_bill_get,
            "print_reciept_configuration": row.print_reciept_configuration,
        })
        doc.insert(ignore_permissions=True)
        created.append((row.user, terminal_id))

    frappe.db.commit()

    if created:
        summary = "\n".join(
            "  - user {0} -> terminal {1}".format(u or "(none)", t) for u, t in created
        )
        frappe.log_error(
            title="Alhamrani Device Map -> Alhamrani Terminal migration",
            message=(
                "Migrated {0} terminal(s). These are NOT yet attached to any "
                "POS Profile -- go to each relevant POS Profile and add the "
                "appropriate terminal(s) under 'Alhamrani Terminals'.\n\n{1}"
            ).format(len(created), summary),
        )