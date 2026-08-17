# Copyright (c) 2026, ERPGulf and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document

IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
COM_PORT_RE = re.compile(r"^COM\d{1,3}$", re.IGNORECASE)


class AlhamraniDeviceMap(Document):
	def validate(self):
		self.validate_ecr_no()
		self.validate_terminal_address()
		self.validate_duplicate_address()
		self.validate_not_also_geidea()

	def validate_ecr_no(self):
		"""AU spec: N, fixed 3 digits."""
		ecr_no = (self.ecr_no or "").strip()
		if not ecr_no.isdigit() or len(ecr_no) != 3:
			frappe.throw(
				_("ECR No must be exactly 3 digits (AU spec). Got {0}.").format(ecr_no or "(blank)")
			)

	def validate_terminal_address(self):
		"""IPv4 for Wi-Fi/Ethernet, COM port for USB/Serial."""
		address = (self.terminal_address or "").strip()

		if self.connection == "Wi-Fi / Ethernet":
			if not IPV4_RE.match(address):
				frappe.throw(
					_("Terminal Address must be an IPv4 address for a Wi-Fi / Ethernet connection, e.g. 192.168.1.5.")
				)
		elif self.connection == "USB / Serial":
			if not COM_PORT_RE.match(address):
				frappe.throw(
					_("Terminal Address must look like COM4 for a USB / Serial connection.")
				)

	def validate_duplicate_address(self):
		"""Reject a duplicate address only for Wi-Fi. COM4 on two different till
		PCs is two different physical ports, not a conflict."""
		if self.connection != "Wi-Fi / Ethernet":
			return

		existing = frappe.db.get_value(
			"Alhamrani Device Map",
			{
				"terminal_address": self.terminal_address,
				"connection": "Wi-Fi / Ethernet",
				"name": ("!=", self.name),
			},
			"name",
		)
		if existing:
			frappe.throw(
				_("Terminal Address {0} is already mapped on {1}.").format(self.terminal_address, existing)
			)

	def validate_not_also_geidea(self):
		"""A till is one provider or the other, never both. With both enabled a
		Geidea-approved sale would still hit the Alhamrani submit guard."""
		if not self.device_enabled:
			return

		if frappe.db.get_value("GEIdea Device Map", {"user": self.user}, "custom_device_enabled"):
			frappe.throw(
				_("{0} already has Geidea enabled. A till can use only one provider.").format(self.user)
			)