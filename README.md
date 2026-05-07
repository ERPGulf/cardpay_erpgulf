# GEIDEA – ERPNext Credit Card Payment Integration

> Seamless payment processing between **ERPNext / POS Awesome** and **GEIDEA** credit card terminals via MQTT.

---

## Overview

This project bridges ERPNext (and its POS Awesome module) with GEIDEA credit card machines. When a sale is made, the transaction amount is pushed to the physical credit card terminal over MQTT. The terminal processes the payment and sends the result back directly to the ERPNext server via a REST callback API.

**Repositories**

| Component | Repository | Technology |
|-----------|-----------|------------|
| Backend (ERPNext App) | [cardpay_erpgulf](https://github.com/ERPGulf/cardpay_erpgulf) | Python / Frappe Framework |
| Frontend (Android App) | [geidea-claudion](https://github.com/ERPGulf/geidea-claudion) | Kotlin / Android |


**Architecture and Installation**
https://app.erpgulf.com/en/articles/geidea-erp-next-credit-card-payment-integration

**Cardpay ERPNext Integration Manual**
https://docs.claudion.com/Claudion-Docs/POSApp

**Video Presentaion**
https://docs.claudion.com/files/pos%20app%20-%20Made%20with%20Clipchamp%20(1)%20(1)%20(1)%20(1).mp4


## License & Copyright

This software is **proprietary and copyright protected**. It is not open-source.

© ERPGulf & Claudion. All rights reserved. Unauthorized copying, distribution, or modification is strictly prohibited.

For licensing inquiries, visit [erpgulf.com](https://erpgulf.com) or [claudion.com](https://claudion.com).

---

## Contact

| | |
|---|---|
| **Support** | [support@erpgulf.com](mailto:support@erpgulf.com) |
| **Sales** | [sales@erpgulf.com](mailto:sales@erpgulf.com) |
| **Website** | [erpgulf.com](https://erpgulf.com) · [claudion.com](https://claudion.com) |