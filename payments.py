import uuid
import json
from urllib.parse import urljoin

try:
    import requests
except Exception:
    requests = None


def _enabled(value):
    return str(value or "0").strip().lower() in {"1", "true", "yes", "on"}


class PaymentError(Exception):
    pass


class DemoPaymentClient:
    provider = "demo"

    def __init__(self, settings=None):
        self.settings = settings or {}

    def is_enabled(self):
        return False

    def create_payment(self, order, return_url=None, idempotence_key=None):
        provider_order_id = f"DEMO-{order['id']}-{uuid.uuid4().hex[:10]}"
        demo_url = (self.settings.get("demo_payment_url") or "").strip() or f"/api/payments/demo-pay/{order['id']}"
        return {
            "provider": "demo",
            "provider_order_id": provider_order_id,
            "payment_url": demo_url,
            "status": "pending",
            "raw_response": {
                "mode": "demo",
                "message": "Онлайн-оплата выключена или выбран demo-провайдер."
            }
        }

    def get_payment_status(self, provider_order_id):
        return {"status": "pending", "paid": False, "raw": {"mode": "demo"}}

    def check_connection(self):
        return {"ok": True, "provider": "demo", "message": "Demo-режим готов"}

    def refund_payment(self, provider_order_id, amount, order_id=None, reason=None):
        return {
            "refund_id": f"DEMO-REFUND-{uuid.uuid4().hex[:10]}",
            "status": "succeeded",
            "raw_response": {"mode": "demo", "amount": int(amount)}
        }


class YooKassaPaymentClient:
    provider = "yookassa"

    def __init__(self, settings):
        self.settings = settings or {}
        self.api_url = (self.settings.get("yookassa_api_url") or "https://api.yookassa.ru/v3").rstrip("/")

    def is_enabled(self):
        return _enabled(self.settings.get("enabled"))

    def _credentials(self):
        shop_id = (self.settings.get("yookassa_shop_id") or "").strip()
        secret_key = (self.settings.get("yookassa_secret_key") or "").strip()
        if not shop_id:
            raise PaymentError("Не указан Shop ID ЮKassa")
        if not secret_key:
            raise PaymentError("Не указан Secret Key ЮKassa")
        return shop_id, secret_key

    def _ensure_requests(self):
        if requests is None:
            raise PaymentError("Не установлен пакет requests. Выполните: pip install requests")

    def _headers(self, idempotence_key=None):
        headers = {"Content-Type": "application/json"}
        if idempotence_key:
            headers["Idempotence-Key"] = idempotence_key
        return headers

    def _json_or_error(self, response, service="ЮKassa"):
        try:
            data = response.json()
        except Exception:
            raise PaymentError(f"{service} вернула не JSON: HTTP {response.status_code}: {response.text[:300]}")
        if response.status_code >= 400:
            description = data.get("description") or data.get("code") or data
            raise PaymentError(f"Ошибка {service}: HTTP {response.status_code}: {description}")
        return data

    def create_payment(self, order, return_url=None, idempotence_key=None):
        self._ensure_requests()
        shop_id, secret_key = self._credentials()
        return_url = (return_url or self.settings.get("return_url") or "").strip()
        if not return_url:
            raise PaymentError("Не указан Return URL для ЮKassa")

        idem = idempotence_key or f"vgmu-pay-{order['id']}-{uuid.uuid4().hex}"
        payload = {
            "amount": {"value": f"{int(order['total']):.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url, "locale": "ru_RU"},
            "description": f"Заказ #{order['id']} ВГМУ Буфет",
            "metadata": {
                "local_order_id": str(order["id"]),
                "branch_id": str(order.get("branch_id", ""))
            }
        }

        method = (self.settings.get("yookassa_payment_method") or "auto").strip()
        test_mode = _enabled(self.settings.get("yookassa_test_mode"))
        if test_mode and method == "sbp":
            raise PaymentError("В тестовом магазине ЮKassa СБП/QR недоступен. Выберите авто или банковскую карту.")
        if method in {"bank_card", "yoo_money", "sbp"}:
            payload["payment_method_data"] = {"type": method}

        response = requests.post(
            f"{self.api_url}/payments",
            auth=(shop_id, secret_key),
            headers=self._headers(idem),
            json=payload,
            timeout=20
        )
        data = self._json_or_error(response)
        payment_id = data.get("id")
        confirmation_url = (data.get("confirmation") or {}).get("confirmation_url")
        if not payment_id or not confirmation_url:
            raise PaymentError(f"ЮKassa не вернула id/confirmation_url: {data}")

        return {
            "provider": "yookassa",
            "provider_order_id": payment_id,
            "payment_url": confirmation_url,
            "status": data.get("status") or "pending",
            "idempotence_key": idem,
            "raw_response": data
        }

    def get_payment_status(self, provider_order_id):
        self._ensure_requests()
        shop_id, secret_key = self._credentials()
        response = requests.get(
            f"{self.api_url}/payments/{provider_order_id}",
            auth=(shop_id, secret_key),
            timeout=20
        )
        data = self._json_or_error(response)
        status = data.get("status") or "unknown"
        return {
            "status": status,
            "paid": status == "succeeded",
            "canceled": status == "canceled",
            "raw": data
        }

    def check_connection(self):
        self._ensure_requests()
        shop_id, secret_key = self._credentials()
        response = requests.get(
            f"{self.api_url}/payments",
            params={"limit": 1},
            auth=(shop_id, secret_key),
            timeout=20
        )
        data = self._json_or_error(response)
        return {
            "ok": data.get("type") == "list" or isinstance(data.get("items"), list),
            "provider": "yookassa",
            "message": "ЮKassa отвечает, Shop ID и Secret Key приняты"
        }

    def refund_payment(self, provider_order_id, amount, order_id=None, reason=None):
        self._ensure_requests()
        shop_id, secret_key = self._credentials()
        idem = f"vgmu-refund-{order_id or 'order'}-{uuid.uuid4().hex}"
        payload = {
            "payment_id": provider_order_id,
            "amount": {"value": f"{int(amount):.2f}", "currency": "RUB"},
            "description": reason or f"Возврат заказа #{order_id or ''}"
        }
        response = requests.post(
            f"{self.api_url}/refunds",
            auth=(shop_id, secret_key),
            headers=self._headers(idem),
            json=payload,
            timeout=20
        )
        data = self._json_or_error(response)
        return {
            "refund_id": data.get("id"),
            "status": data.get("status") or "pending",
            "raw_response": data,
            "idempotence_key": idem
        }


class SberPaymentClient:
    provider = "sber"

    def __init__(self, settings):
        self.settings = settings or {}

    def is_enabled(self):
        return _enabled(self.settings.get("enabled"))

    def _get_api_base(self):
        api_url = (self.settings.get("api_url") or "").strip()
        test_api_url = (self.settings.get("test_api_url") or "").strip()
        base = api_url or test_api_url
        if not base:
            raise PaymentError("Не указан API URL Сбера")
        return base.rstrip("/") + "/"

    def _endpoint(self, method_name):
        return urljoin(self._get_api_base(), method_name)

    def _credentials(self):
        username = (self.settings.get("username") or "").strip()
        password = (self.settings.get("password") or "").strip()
        if not username:
            raise PaymentError("Не указан API логин Сбера")
        if not password:
            raise PaymentError("Не указан API пароль Сбера")
        return username, password

    def create_payment(self, order, return_url=None, idempotence_key=None):
        if not self.is_enabled():
            return DemoPaymentClient(self.settings).create_payment(order, return_url, idempotence_key=idempotence_key)
        if requests is None:
            raise PaymentError("Не установлен пакет requests. Выполните: pip install requests")
        username, password = self._credentials()
        return_url = (return_url or self.settings.get("return_url") or "").strip()
        fail_url = (self.settings.get("fail_url") or "").strip()
        if not return_url:
            raise PaymentError("Не указан Return URL после успешной оплаты")
        if not fail_url:
            raise PaymentError("Не указан Fail URL после ошибки оплаты")

        provider_order_id = f"VGMU-{order['id']}-{uuid.uuid4().hex[:8]}"
        payload = {
            "userName": username,
            "password": password,
            "orderNumber": provider_order_id,
            "amount": int(order["total"]) * 100,
            "returnUrl": return_url,
            "failUrl": fail_url,
            "description": f"Заказ #{order['id']} ВГМУ Буфет",
            "jsonParams": json.dumps({"local_order_id": str(order["id"]), "branch_id": str(order.get("branch_id", ""))}, ensure_ascii=False)
        }
        response = requests.post(self._endpoint("register.do"), data=payload, timeout=20)
        try:
            data = response.json()
        except Exception:
            raise PaymentError(f"Сбер вернул не JSON: HTTP {response.status_code}: {response.text[:300]}")
        if response.status_code >= 400:
            raise PaymentError(f"Ошибка HTTP Сбера {response.status_code}: {data}")
        if data.get("errorCode") not in [None, "0", 0]:
            raise PaymentError(f"Ошибка Сбера: {data.get('errorCode')} {data.get('errorMessage') or data.get('error')}")
        bank_order_id = data.get("orderId")
        form_url = data.get("formUrl")
        if not bank_order_id or not form_url:
            raise PaymentError(f"Сбер не вернул orderId/formUrl: {data}")
        return {"provider": "sber", "provider_order_id": bank_order_id, "payment_url": form_url, "status": "pending", "raw_response": data}

    def get_payment_status(self, provider_order_id):
        if requests is None:
            raise PaymentError("Не установлен пакет requests")
        username, password = self._credentials()
        response = requests.post(self._endpoint("getOrderStatusExtended.do"), data={"userName": username, "password": password, "orderId": provider_order_id}, timeout=20)
        data = response.json()
        paid = str(data.get("orderStatus")) == "2"
        return {"status": "succeeded" if paid else "pending", "paid": paid, "raw": data}

    def check_connection(self):
        self._credentials()
        self._get_api_base()
        return {"ok": True, "provider": "sber", "message": "Настройки Сбера заполнены; проверка реальным тестовым платежом выполняется отдельно"}

    def refund_payment(self, provider_order_id, amount, order_id=None, reason=None):
        raise PaymentError("Автоматический возврат для Сбера в этой версии не настроен. Используйте банковский кабинет или подключите точный API возврата по вашему договору.")

    def verify_callback(self, payload):
        return bool(payload.get("mdOrder") or payload.get("orderNumber") or payload.get("order_id") or payload.get("provider_order_id"))


def build_payment_client(settings, provider_override=None):
    settings = settings or {}
    provider = (provider_override or settings.get("provider") or "demo").strip().lower()
    # provider_override нужен для возврата уже созданного платежа, даже если в админке позже сменили провайдера.
    if not provider_override and not _enabled(settings.get("enabled")):
        return DemoPaymentClient(settings)
    if provider in {"yookassa", "yoo", "юkassa", "юкасса"}:
        return YooKassaPaymentClient(settings)
    if provider == "sber":
        return SberPaymentClient(settings)
    return DemoPaymentClient(settings)
