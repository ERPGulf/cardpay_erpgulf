# # Copyright (c) 2025, ERPGulf and contributors
# # For license information, please see license.txt

# # import frappe
# from frappe.model.document import Document


# class GEIdeaDeviceMap(Document):
# 	pass


import frappe
from frappe import _
from frappe.model.document import Document


class GEIdeaDeviceMap(Document):
	def validate(self):
		# Mirror of the check in Alhamrani Device Map.validate(). A till is one
		# provider or the other, never both -- with both enabled a Geidea-approved
		# sale would still hit the Alhamrani submit guard and be blocked.
		if self.custom_device_enabled and frappe.db.get_value(
			"Alhamrani Device Map", {"user": self.user, "device_enabled": 1}, "name"
		):
			frappe.throw(
				_("{0} already has Alhamrani enabled. A till can use only one provider.").format(self.user)
			)