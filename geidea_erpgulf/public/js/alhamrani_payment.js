/**
 * Alhamrani ECR browser client.
 *
 * Bridges the POS page to AlhamraniServicev2, a Windows service on the till PC.
 * The service opens the socket to the mada terminal over Wi-Fi or USB; the
 * browser only speaks SignalR to localhost.
 *
 * Two protocol facts drive this design:
 *
 *  1. Responses arrive on a BROADCAST callback with no correlation id, so we
 *     correlate on ecr_receipt_no and let the server revalidate the echo.
 *  2. A timeout does NOT mean the card was not charged. Every unresolved send
 *     becomes an Unconfirmed ECR Transaction that a human must close out.
 *
 * Reference: AU doc "ECR-POS Integration WEB/HTML JAVASCRIPT" v1.2.4, MI 25-007.
 */

frappe.provide("alhamrani_payment");

(function () {
	const SIGNALR_LIB = "/assets/geidea_erpgulf/js/vendor/jquery.signalR-2.4.1.min.js";

	let conn = null;
	let hub = null;
	let config = null;

	// ecr_receipt_no -> { resolve, reject, timer, txn }
	const pending = new Map();
	// check/check2 carry no receipt number, so they queue separately.
	const checks = [];

	function libLoaded() {
		return !!(window.jQuery && window.jQuery.signalR);
	}

	async function loadLib() {
		if (libLoaded()) return;
		await new Promise((resolve, reject) => {
			const tag = document.createElement("script");
			tag.src = SIGNALR_LIB;
			tag.onload = resolve;
			tag.onerror = () =>
				reject(
					new Error(
						__("Could not load jquery.signalR-2.4.1.min.js. Place it in alhamrani_payment/public/js/vendor/ and run bench build.")
					)
				);
			document.head.appendChild(tag);
		});
	}

	/** The service may put the JSON in either callback argument. */
	function extractPayload() {
		for (const arg of arguments) {
			if (arg && typeof arg === "object") return arg;
			if (typeof arg === "string") {
				const text = arg.trim();
				if (text.startsWith("{")) {
					try {
						return JSON.parse(text);
					} catch (e) {
						/* keep looking */
					}
				}
			}
		}
		return null;
	}

	function onBroadcast() {
		const res = extractPayload.apply(null, arguments);
		if (!res) {
			console.warn("[ecr] unparseable broadcast", arguments);
			return;
		}

		// check / check2: no receipt number, is_connected present.
		if (!res.ecr_receipt_no && typeof res.is_connected !== "undefined") {
			const waiter = checks.shift();
			if (waiter) {
				clearTimeout(waiter.timer);
				waiter.resolve(res);
			}
			return;
		}

		const key = String(res.ecr_receipt_no);
		const waiter = pending.get(key);
		if (!waiter) {
			// Another tab's transaction, or a late reply to one we already gave
			// up on. Never act on it; the server holds the authoritative state.
			console.warn("[ecr] unmatched response, ignoring", res);
			return;
		}
		pending.delete(key);
		clearTimeout(waiter.timer);
		waiter.resolve(res);
	}

	async function ensureConnected() {
		if (conn && conn.state === $.signalR.connectionState.connected) return;

		await loadLib();
		if (!config) {
			// A missed setup hook should not break a payment. init() with no
			// arguments resolves the shift server-side where it can.
			throw new Error(
				__("The card terminal is not set up for this session. Reopen the POS.")
			);
		}

		conn = $.hubConnection(config.hub_url, { useDefaultPath: false });
		hub = conn.createHubProxy(config.hub_name);

		// Must register before start(), or SignalR never subscribes to the hub.
		hub.on(config.callback_name, onBroadcast);
		conn.error((e) => console.error("[ecr] hub error", e));
		conn.disconnected(() => {
			// Everything in flight is now unknown, not failed.
			for (const [, waiter] of pending) {
				clearTimeout(waiter.timer);
				waiter.reject(Object.assign(new Error(__("Lost connection to the payment service.")), {
					indeterminate: true,
					txn: waiter.txn,
				}));
			}
			pending.clear();
			while (checks.length) {
				const w = checks.shift();
				clearTimeout(w.timer);
				w.reject(new Error(__("Lost connection to the payment service.")));
			}
		});

		try {
			await conn.start({ withCredentials: false });
		} catch (e) {
			throw new Error(
				__("Cannot reach the payment service on this PC. Check that AlhamraniServicev2 is running.")
			);
		}
	}

	/** Send a transaction and wait for its correlated response. */
	function dispatch(keyword, request, txn, timeoutMs) {
		const key = String(request.ecr_receipt_no);
		return new Promise((resolve, reject) => {
			const timer = setTimeout(() => {
				pending.delete(key);
				reject(
					Object.assign(new Error(__("The terminal did not respond in time.")), {
						timedOut: true,
						indeterminate: true,
						txn: txn,
					})
				);
			}, timeoutMs);

			pending.set(key, { resolve, reject, timer, txn });

			hub.invoke("Send", keyword, JSON.stringify(request)).fail((e) => {
				clearTimeout(timer);
				pending.delete(key);
				reject(new Error(__("Could not send to the payment service: {0}", [(e && e.message) || e])));
			});
		});
	}

	Object.assign(alhamrani_payment, {
		/** Load config for this till and connect. Returns unconfirmed items. */
		// async init(pos_profile = null) {
		// 	const r = await frappe.call({
		// 		method: "geidea_erpgulf.alhamrani.get_config",
		// 		args: { pos_profile },
		// 	});
		// 	config = r.message;
		// 	await ensureConnected();
		// 	return config;
		// },
		async init(pos_profile = null, pos_opening_shift = null) {
			// Fall back to resolving the shift ourselves. The POSAwesome call
			// site does not always pass it, and without a shift no terminal can
			// be resolved, so every card payment fails with a null device.
			if (!pos_opening_shift) {
				try {
					const open = await frappe.db.get_list("POS Opening Shift", {
						filters: { status: "Open", docstatus: 1, user: frappe.session.user },
						fields: ["name"],
						order_by: "period_start_date desc",
						limit: 1,
					});
					if (open && open.length) pos_opening_shift = open[0].name;
				} catch (e) {
					console.warn("[ecr] could not resolve the open shift", e);
				}
			}

			const r = await frappe.call({
				method: "geidea_erpgulf.alhamrani.get_config",
				args: { pos_profile, pos_opening_shift },
			});
			config = r.message;
			config.pos_opening_shift = pos_opening_shift;
			await ensureConnected();
			return config;
		},

		is_ready() {
			return !!(conn && conn.state === $.signalR.connectionState.connected);
		},

		get_device() {
			return config && config.device;
		},

		/** check2: confirms reachability and returns the TID. Run on POS open. */
		async check_device() {
			await ensureConnected();
			const t = config.device;
			if (!t) {
				throw new Error(
					__("No terminal is selected for this shift. Choose one before taking a card payment.")
				);
			}
			const request = {
				msg_id: "PUR",
				ecr_no: t.ecr_no,
				ecr_receipt_no: "",
				amount: "",
				field1: "", field2: "", field3: "", field4: "", field5: "",
				port_no_or_ip_adddress: t.address,
				bill_no: "",
			};

			const res = await new Promise((resolve, reject) => {
				const timer = setTimeout(() => {
					const i = checks.findIndex((c) => c.timer === timer);
					if (i > -1) checks.splice(i, 1);
					reject(new Error(__("The terminal at {0} did not answer.", [t.address])));
				}, config.query_timeout_ms);

				checks.push({ resolve, reject, timer });
				hub.invoke("Send", "check2", JSON.stringify(request)).fail((e) => {
					clearTimeout(timer);
					checks.pop();
					reject(new Error(__("Could not reach the payment service.")));
				});
			});

			// Server enforces the TID match and throws on a wrong terminal.
			// await frappe.call({
			// 	method: "geidea_erpgulf.alhamrani.record_tid",
			// 	args: { tid: res.tid || "" },
			// });
			// return res;
			await frappe.call({
				method: "geidea_erpgulf.alhamrani.record_tid",
				args: { tid: res.tid || "", pos_opening_shift: config.pos_opening_shift },
			});
			return res;
		},

		/**
		 * Charge a card. Resolves with the server's decision.
		 *
		 * Rejects with err.indeterminate === true when the outcome is unknown.
		 * In that case the ECR Transaction is already Unconfirmed server-side:
		 * do NOT retry silently, and do NOT submit the invoice.
		 */
		async purchase({ pos_profile, amount, pos_invoice, pos_opening_shift, attempt = 1 }) {
			await ensureConnected();

			const begun = await frappe.call({
				method: "geidea_erpgulf.alhamrani.begin",
				args: {
					amount,
					pos_invoice,
					pos_profile,
					pos_opening_shift: pos_opening_shift || config.pos_opening_shift,
					msg_id: "PUR",
					attempt,
				},
			});
			// async purchase({ pos_profile, amount, pos_invoice, attempt = 1 }) {
			// 	await ensureConnected();

			// const begun = await frappe.call({
			// 	method: "geidea_erpgulf.alhamrani.begin",
			// 	args: { amount, pos_invoice, pos_profile, msg_id: "PUR", attempt },
			// });
			const { txn, request } = begun.message;

			let raw;
			try {
				raw = await dispatch("transaction", request, txn, config.purchase_timeout_ms);
			} catch (err) {
				if (err.indeterminate) {
					// Best effort: stop the terminal prompting, then park the record.
					try {
						await alhamrani_payment.cancel();
					} catch (e) {
						/* the terminal may already be idle */
					}
					await frappe.call({
						method: "geidea_erpgulf.alhamrani.mark_unconfirmed",
						args: { txn, reason: err.message },
					});
					err.txn = txn;
				}
				throw err;
			}

			const decided = await frappe.call({
				method: "geidea_erpgulf.alhamrani.finish",
				args: { txn, response: JSON.stringify(raw) },
			});
			return Object.assign({ txn }, decided.message);
		},

		/** Refund against an earlier transaction. Original RRN/date/PAN are looked
		 * up server-side in begin() via _original_card_details() -- the browser
		 * never needs to know them. */

		// async refund({ amount, pos_invoice, pos_opening_shift }) {
		// 	await ensureConnected();
		// 	const begun = await frappe.call({
		// 		method: "geidea_erpgulf.alhamrani.begin",
		// 		args: {
		// 			amount,
		// 			pos_invoice,
		// 			pos_opening_shift: pos_opening_shift || config.pos_opening_shift,
		// 			msg_id: "REF",
		// 		},
		// 	});
		// 	const { txn, request } = begun.message;
		// 	const raw = await dispatch("transaction", request, txn, config.purchase_timeout_ms);
		// 	const decided = await frappe.call({
		// 		method: "geidea_erpgulf.alhamrani.finish",
		// 		args: { txn, response: JSON.stringify(raw) },
		// 	});
		// 	return Object.assign({ txn }, decided.message);
		// },
		async refund({ amount, pos_invoice, pos_opening_shift }) {
		await ensureConnected();
		const begun = await frappe.call({
			method: "geidea_erpgulf.alhamrani.begin",
			args: {
			amount,
			pos_invoice,
			pos_opening_shift: pos_opening_shift || config.pos_opening_shift,
			msg_id: "REF",
			},
		});
		const { txn, request } = begun.message;

		let raw;
		try {
			raw = await dispatch("transaction", request, txn, config.purchase_timeout_ms);
		} catch (err) {
			if (err.indeterminate) {
			// Same as purchase(): a failed/timed-out refund leaves the physical
			// terminal waiting for a response that will never come. Without this
			// cancel, the terminal rejects every subsequent PUR/REF with
			// "ALREADY IN TXN" and even stops answering check2, since from its
			// own state machine it's still mid-transaction. Best effort: tell it
			// to abandon the pending refund, then park the record as Unconfirmed
			// so a human resolves it (a refund left ambiguous must never be
			// silently retried -- that's how a customer gets refunded twice).
			try {
				await alhamrani_payment.cancel();
			} catch (e) {
				// Terminal may already be idle -- nothing to cancel.
			}
			await frappe.call({
				method: "geidea_erpgulf.alhamrani.mark_unconfirmed",
				args: { txn, reason: err.message },
			});
			err.txn = txn;
			}
			throw err;
		}

		const decided = await frappe.call({
			method: "geidea_erpgulf.alhamrani.finish",
			args: { txn, response: JSON.stringify(raw) },
		});
		return Object.assign({ txn }, decided.message);
		},

		/**
		 * Ask the terminal what happened to a bill number.
		 *
		 * Only works where the terminal supports it (AU MI 25-007 restricts this
		 * to Move2500) and only until the terminal journal is cleared by a
		 * reboot, reset or replacement. Treat a miss as "unknown", not "not charged".
		 */
		async query_bill(bill_no) {
			await ensureConnected();
			const t = config.device;
			if (!t) {
				throw new Error(__("No terminal is selected for this shift."));
			}
			if (!t.supports_bill_get) {
				throw new Error(
					__("This terminal model does not support lookup by bill number. Check the outcome on the terminal itself.")
				);
			}
			const request = {
				msg_id: "GET",
				ecr_no: t.ecr_no,
				ecr_receipt_no: "0001EEEEEE", // required trigger value, not a correlation key
				amount: "",
				field1: "", field2: "", field3: "", field4: "", field5: "",
				port_no_or_ip_adddress: t.address,
				bill_no: String(bill_no),
			};
			return dispatch("transaction", request, null, config.query_timeout_ms);
		},

		/** End of day totals. */
		// async reconcile(pos_profile) {
		// 	await ensureConnected();
		// 	const begun = await frappe.call({
		// 		method: "geidea_erpgulf.alhamrani.begin",
		// 		args: { pos_profile, amount: 0, msg_id: "REC" },
		// 	});
		async reconcile(pos_profile) {
			await ensureConnected();
			const begun = await frappe.call({
				method: "geidea_erpgulf.alhamrani.begin",
				args: {
					pos_profile,
					pos_opening_shift: config.pos_opening_shift,
					amount: 0,
					msg_id: "REC",
				},
			});
			const { txn, request } = begun.message;
			request.msg_id = "REC";
			request.amount = "";
			return dispatch("transaction", request, txn, config.purchase_timeout_ms);
		},

		/** Undocumented but present in the vendor sample. Used on timeout. */
		async cancel() {
			if (!alhamrani_payment.is_ready()) return;
			const t = config.device;
			if (!t) return;   // nothing selected, nothing to cancel
			return hub.invoke(
				"Send",
				"cancel",
				JSON.stringify({
					msg_id: "",
					ecr_no: "",
					ecr_receipt_no: "",
					amount: "",
					field1: "", field2: "", field3: "", field4: "", field5: "",
					port_no_or_ip_adddress: t.address,
					bill_no: "",
				})
			);
		},

		// async unconfirmed() {
		// 	const r = await frappe.call({
		// 		method: "geidea_erpgulf.alhamrani.get_unconfirmed",
		// 	});
		// 	return r.message || [];
		// },
		async unconfirmed() {
			const r = await frappe.call({
				method: "geidea_erpgulf.alhamrani.get_unconfirmed",
				args: { pos_opening_shift: config.pos_opening_shift },
			});
			return r.message || [];
		},
	});
})();