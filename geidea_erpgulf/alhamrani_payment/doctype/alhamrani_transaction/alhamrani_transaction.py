# Copyright (c) 2026, ERPGulf and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

# These records are the audit trail for money that may have moved. Deleting
# one does not undo a charge at the terminal or in the bank statement.
BLOCKED_STATUSES = ("Approved", "Unconfirmed")


class AlhamraniTransaction(Document):
	def on_trash(self):
		if self.status in BLOCKED_STATUSES:
			frappe.throw(
				_(
					"Cannot delete {0}: status is {1}. This record is the audit trail for "
					"money that may have moved at the terminal. Resolve it first if it is "
					"Unconfirmed."
				).format(self.name, self.status)
			)