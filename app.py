from flask import Flask, render_template, request, jsonify, session, redirect
from flask_socketio import SocketIO, join_room
from werkzeug.utils import secure_filename
try:
    from PIL import Image, ImageOps
except Exception:
    Image = None
    ImageOps = None
from pathlib import Path
from io import BytesIO
from database import Database
from payments import SberPaymentClient, YooKassaPaymentClient, DemoPaymentClient, build_payment_client
import secrets
import random
import os
import shutil
import re
import threading
import time
from datetime import datetime, timedelta, timezone

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

SECRET_FILE = BASE_DIR / ".secret_key"

if SECRET_FILE.exists():
    app.secret_key = SECRET_FILE.read_text(encoding="utf-8").strip()
else:
    app.secret_key = secrets.token_hex(32)
    SECRET_FILE.write_text(app.secret_key, encoding="utf-8")

app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=8 * 1024 * 1024
)

MSK_TZ = timezone(timedelta(hours=3))

# Python 3.14 is not compatible with the old eventlet stack used in earlier builds.
# Use Flask-SocketIO's native threaded mode instead. It works on Windows and does
# not monkey-patch the standard threading/ssl modules.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

BACKUP_DIR = BASE_DIR / "backups"

db = Database()

ADMIN_PASSWORD = os.getenv("VGMU_ADMIN_PASSWORD", "admin123")

AUTH_ATTEMPTS = {}
AUTH_LOCK = threading.Lock()
AUTH_WINDOW_SECONDS = 300
AUTH_MAX_FAILURES = 8

def _auth_key(scope):
    return f"{scope}:{request.remote_addr or 'unknown'}"

def auth_is_blocked(scope):
    now = time.time()
    key = _auth_key(scope)
    with AUTH_LOCK:
        attempts = [ts for ts in AUTH_ATTEMPTS.get(key, []) if now - ts < AUTH_WINDOW_SECONDS]
        AUTH_ATTEMPTS[key] = attempts
        return len(attempts) >= AUTH_MAX_FAILURES

def auth_failed(scope):
    key = _auth_key(scope)
    with AUTH_LOCK:
        AUTH_ATTEMPTS.setdefault(key, []).append(time.time())

def auth_succeeded(scope):
    key = _auth_key(scope)
    with AUTH_LOCK:
        AUTH_ATTEMPTS.pop(key, None)

@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), autoplay=(self), screen-wake-lock=(self)")
    if request.path == "/static/sw.js":
        response.headers["Service-Worker-Allowed"] = "/"
    return response

def normalize_phone(phone):
    phone = re.sub(r"\D", "", phone or "")

    if len(phone) == 11 and phone.startswith("8"):
        phone = "7" + phone[1:]

    if len(phone) != 11 or not phone.startswith("7"):
        return None

    return "+" + phone


def save_image(file):
    if not file or not file.filename:
        return ""

    ext = file.filename.rsplit(".", 1)[-1].lower()

    if ext not in ["jpg", "jpeg", "png", "webp", "gif", "bmp"]:
        return ""

    # Единый стандарт для фото блюд:
    # любые JPG/PNG/BMP/WEBP автоматически конвертируются в WEBP 512x384.
    name = secure_filename(f"{secrets.token_hex(8)}.webp")
    path = UPLOAD_DIR / name

    if Image and ImageOps:
        try:
            img = Image.open(file.stream).convert("RGB")

            # Центрируем и подгоняем под единый размер карточек.
            img = ImageOps.fit(
                img,
                (512, 384),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5)
            )

            img.save(
                path,
                format="WEBP",
                quality=80,
                optimize=True,
                method=6
            )

            return name

        except Exception:
            try:
                file.stream.seek(0)
            except Exception:
                pass

    # fallback без Pillow
    file.save(path)
    return name

def get_cart_token():
    if "cart_token" not in session:
        session["cart_token"] = secrets.token_hex(16)
    return session["cart_token"]


def emit_all(branch_id=None):
    socketio.emit("update", {"scope": "all"})

    if branch_id:
        socketio.emit(
            "update",
            {"scope": "branch", "branch_id": branch_id},
            room=f"branch_{branch_id}"
        )


def expire_orders_loop():
    last_writeoff_date = None
    last_integrity_scan = 0.0
    last_payment_reconcile = 0.0

    while True:
        try:
            changed_branch_ids = db.expire_old_orders()
            payment_hold_branches = db.expire_payment_holds()
            closed_count = db.auto_close_empty_positions()

            if closed_count:
                emit_all()

            monotonic_now = time.time()

            # Раз в минуту сверяем зависшие pending-платежи с платёжным провайдером.
            # Ошибка одного платежа не останавливает обработку остальных.
            if monotonic_now - last_payment_reconcile >= 60:
                last_payment_reconcile = monotonic_now
                for pending_order_id in db.get_pending_order_ids(limit=10):
                    fingerprint = f"payment_reconcile:order:{pending_order_id}"
                    try:
                        reconcile_payment_status(pending_order_id)
                        db.resolve_incident_by_fingerprint(fingerprint)
                    except Exception as reconcile_error:
                        db.record_incident(
                            fingerprint,
                            "payment",
                            "Не удалось автоматически сверить статус онлайн-оплаты",
                            "warning",
                            pending_order_id,
                            str(reconcile_error)[:500]
                        )

            if monotonic_now - last_integrity_scan >= 60:
                last_integrity_scan = monotonic_now
                db.scan_state_incidents()

            now = datetime.now(MSK_TZ)

            if now.hour == 20 and now.minute == 0:
                current_date = now.strftime("%Y-%m-%d")

                if last_writeoff_date != current_date:
                    db.daily_writeoff()
                    last_writeoff_date = current_date
                    emit_all()

            if changed_branch_ids or payment_hold_branches:
                emit_all()

                for branch_id in set(changed_branch_ids + payment_hold_branches):
                    emit_all(branch_id)

        except Exception as e:
            print("expire_orders_loop error:", e)

        time.sleep(20)

@app.route("/")
def client():
    return render_template("client.html")


@app.route("/operator")
def operator():
    if not session.get("kitchen_id"):
        return redirect("/operator-login")

    return render_template("operator.html")


@app.route("/operator-login")
def operator_login_page():
    return render_template("operator_login.html")


@app.route("/admin")
def admin():
    return render_template("admin.html")




@app.route("/api/branches")
def branches():
    return jsonify(db.get_branches())




@app.route("/api/schedule")
def public_schedule():
    return jsonify(db.get_branch_schedule())


@app.route("/api/auth", methods=["POST"])
def auth():
    phone = normalize_phone((request.json or {}).get("phone", ""))

    if not phone:
        return jsonify({
            "error": "Введите номер в формате +79081234567 или 89081234567"
        }), 400

    user = db.get_user_by_phone(phone)

    if not user:
        user = db.get_user(db.create_user(phone))

    session.permanent = True
    session["user_id"] = user["id"]

    return jsonify(user)


@app.route("/api/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    return jsonify({"success": True})


@app.route("/api/me")
def me():
    uid = session.get("user_id")

    if not uid:
        return jsonify({"error": "Не авторизован"}), 401

    return jsonify(db.get_user(uid))


@app.route("/api/menu")
def menu():
    branch_id = int(request.args.get("branch_id", 1))
    return jsonify(db.get_menu_for_branch(branch_id, get_cart_token()))


@app.route("/api/combos")
def combos():
    return jsonify(db.get_combos())


@app.route("/api/orders", methods=["POST"])
def create_order():
    uid = session.get("user_id")

    if not uid:
        return jsonify({"error": "Сначала войдите по номеру телефона"}), 401

    data = request.json or {}

    try:
        branch_id = int(data.get("branch_id"))
        order_id = db.create_order(
            uid,
            branch_id,
            data.get("items", []),
            token=get_cart_token(),
            pickup_time=data.get("pickup_time") or None
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 400

    emit_all(branch_id)

    return jsonify({"success": True, "order_id": order_id})


@app.route("/api/payments/create", methods=["POST"])
def create_payment():
    uid = session.get("user_id")

    if not uid:
        return jsonify({"error": "Сначала войдите по номеру телефона"}), 401

    data = request.json or {}
    order_id = int(data.get("order_id") or 0)
    order = db.get_order(order_id)

    if not order or order.get("user_id") != uid:
        return jsonify({"error": "Заказ не найден"}), 404

    if order.get("payment_status") == "paid":
        return jsonify({"success": True, "already_paid": True})

    settings = db.get_payment_settings()

    # Повторный клик не создаёт второй платёж, пока действует текущая бронь.
    if order.get("payment_status") == "pending" and order.get("payment_hold_expires_at"):
        previous = db.get_latest_payment_for_order(order_id)
        if previous and previous.get("status") == "pending" and previous.get("payment_url"):
            return jsonify({
                "success": True,
                "payment_url": previous["payment_url"],
                "provider_order_id": previous.get("provider_order_id"),
                "reused": True,
                "hold_expires_at": order.get("payment_hold_expires_at")
            })

    hold_minutes = int(settings.get("payment_hold_minutes") or 10)

    try:
        hold_expires_at = db.start_payment_hold(order_id, hold_minutes)
        client = build_payment_client(settings)
        default_return_url = request.host_url.rstrip("/") + f"/payment-result?order_id={order_id}"
        attempt_no = db.get_payment_attempt_count(order_id) + 1
        idempotence_key = f"vgmu-order-{order_id}-attempt-{attempt_no}"
        result = client.create_payment(
            order,
            return_url=(settings.get("return_url") or default_return_url),
            idempotence_key=idempotence_key
        )

        db.create_payment_record(
            order_id=order_id,
            provider=result["provider"],
            provider_order_id=result["provider_order_id"],
            amount=order["total"],
            payment_url=result["payment_url"],
            raw_response=result["raw_response"]
        )

        return jsonify({
            "success": True,
            "payment_url": result["payment_url"],
            "provider_order_id": result["provider_order_id"],
            "hold_expires_at": hold_expires_at
        })

    except Exception as e:
        try:
            db.release_payment_hold(order_id, "Не удалось открыть платёжный шлюз")
        except Exception:
            pass
        return jsonify({"error": str(e)}), 400


@app.route("/api/payments/demo-pay/<int:order_id>")
def demo_pay(order_id):
    uid = session.get("user_id")

    if not uid:
        return jsonify({"error": "Сначала войдите"}), 401

    order = db.get_order(order_id)

    if not order or order.get("user_id") != uid:
        return jsonify({"error": "Заказ не найден"}), 404

    try:
        db.finalize_paid_order(order_id)
    except Exception as e:
        return f"<h1>Тестовая оплата не подтверждена</h1><p>{str(e)}</p><a href='/'>Вернуться</a>", 409
    emit_all(order.get("branch_id"))

    return """
    <html><head><meta charset='utf-8'><title>Оплата</title></head>
    <body style='font-family:Arial;padding:40px'>
        <h1>Тестовая оплата прошла</h1>
        <p>Это demo-режим. Реальный Сбер API ещё не подключён.</p>
        <a href='/'>Вернуться к заказам</a>
        <script>
            setTimeout(function(){ window.location.href='/' }, 1200)
        </script>
    </body></html>
    """


@app.route("/api/payments/sber/callback", methods=["POST"])
def sber_callback():
    payload = request.json or request.form.to_dict()
    settings = db.get_payment_settings()
    client = SberPaymentClient(settings)

    if not client.verify_callback(payload):
        return jsonify({"error": "bad callback"}), 400

    provider_order_id = payload.get("mdOrder") or payload.get("orderId") or payload.get("provider_order_id")
    local_order_id = payload.get("order_id") or payload.get("local_order_id")

    order = None

    if local_order_id:
        try:
            order = db.get_order(int(local_order_id))
        except Exception:
            order = None

    if not order and provider_order_id:
        order = db.get_order_by_provider_order_id(provider_order_id)

    if not order:
        return jsonify({"error": "order not found"}), 404

    # Если банк включён, дополнительно спрашиваем статус у Сбера.
    # Для некоторых callback-форматов это надёжнее, чем доверять входящему status.
    paid = False

    try:
        if provider_order_id and client.is_enabled():
            status = client.get_order_status(provider_order_id)
            paid = bool(status.get("paid"))
        else:
            paid = True
    except Exception:
        # Если банк прислал явный успешный статус, разрешаем пометить как оплаченный.
        raw_status = str(payload.get("status") or payload.get("operation") or "").lower()
        paid = raw_status in ["1", "2", "deposited", "approved", "paid"]

    if paid:
        try:
            db.finalize_paid_order(order["id"], provider_order_id=provider_order_id)
        except Exception as e:
            db.mark_refund_required(order["id"], provider_order_id, "Сбер: оплата пришла после потери резерва: " + str(e))
        emit_all(order.get("branch_id"))

    return jsonify({"success": True, "paid": paid})


def reconcile_payment_status(order_id, user_id=None):
    order = db.get_order(order_id)
    if not order:
        raise Exception("Заказ не найден")
    if user_id is not None and order.get("user_id") != user_id:
        raise Exception("Нет доступа к заказу")

    if order.get("payment_status") in ("paid", "refunded"):
        return {"payment_status": order.get("payment_status"), "provider_status": order.get("payment_status")}

    payment = db.get_latest_payment_for_order(order_id)
    if not payment or not payment.get("provider_order_id"):
        return {"payment_status": order.get("payment_status"), "provider_status": "not_created"}

    provider = payment.get("provider") or "demo"
    if provider == "demo":
        return {"payment_status": order.get("payment_status"), "provider_status": payment.get("status") or "pending"}

    settings = db.get_payment_settings()
    client = build_payment_client(settings, provider_override=provider)
    status = client.get_payment_status(payment.get("provider_order_id"))

    if status.get("paid"):
        try:
            db.finalize_paid_order(order_id, provider_order_id=payment.get("provider_order_id"))
        except Exception as reserve_error:
            if provider == "yookassa":
                try:
                    refund = client.refund_payment(payment.get("provider_order_id"), order["total"], order_id=order_id, reason="Автовозврат: товар закончился после истечения брони")
                    db.mark_order_refunded(order_id, payment.get("provider_order_id"), refund.get("refund_id"), refund.get("status"), refund.get("raw_response"), cancel_order=True)
                except Exception as refund_error:
                    db.mark_refund_required(order_id, payment.get("provider_order_id"), f"{reserve_error}; автовозврат не выполнен: {refund_error}")
            else:
                db.mark_refund_required(order_id, payment.get("provider_order_id"), str(reserve_error))
    elif status.get("canceled"):
        db.mark_payment_canceled(order_id, provider_order_id=payment.get("provider_order_id"))

    fresh = db.get_order(order_id)
    emit_all(fresh.get("branch_id") if fresh else None)
    return {"payment_status": fresh.get("payment_status") if fresh else "unknown", "provider_status": status.get("status")}


@app.route("/api/payments/status/<int:order_id>")
def payment_status(order_id):
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "Сначала войдите по номеру телефона"}), 401
    try:
        return jsonify(reconcile_payment_status(order_id, user_id=uid))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/payment-result")
def payment_result():
    order_id = request.args.get("order_id", "")
    return f"""
    <html><head><meta charset='utf-8'><title>Проверка оплаты</title>
    <meta name='viewport' content='width=device-width,initial-scale=1'></head>
    <body style='font-family:Arial;padding:28px;max-width:620px;margin:auto;background:#f8fafc'>
        <div style='background:white;padding:24px;border-radius:18px;box-shadow:0 8px 30px #0001'>
            <h1>Проверяем оплату</h1>
            <p id='status'>Связываемся с платёжной системой…</p>
            <a href='/'>Вернуться к заказам</a>
        </div>
        <script>
        (async function(){{
            const orderId = {order_id if str(order_id).isdigit() else 'null'};
            if(!orderId){{ document.getElementById('status').innerText='Вернитесь в приложение — статус заказа обновится там.'; return; }}
            try{{
                const r=await fetch('/api/payments/status/'+orderId,{{credentials:'same-origin'}});
                const d=await r.json();
                if(d.payment_status==='paid') document.getElementById('status').innerText='✅ Оплата подтверждена. Заказ оплачен.';
                else if(d.payment_status==='refunded') document.getElementById('status').innerText='↩️ Платёж возвращён.';
                else if(d.payment_status==='refund_required') document.getElementById('status').innerText='⚠️ Платёж требует ручной проверки возврата.';
                else document.getElementById('status').innerText='⏳ Платёж ещё обрабатывается. Статус можно проверить в заказах.';
            }}catch(e){{ document.getElementById('status').innerText='Не удалось проверить автоматически. Откройте вкладку «Заказы».'; }}
            setTimeout(function(){{location.href='/'}},2200);
        }})();
        </script>
    </body></html>
    """


@app.route("/api/payments/yookassa/webhook", methods=["POST"])
def yookassa_webhook():
    payload = request.json or {}
    event = str(payload.get("event") or "")
    obj = payload.get("object") or {}
    provider_order_id = obj.get("id")

    if not provider_order_id:
        return jsonify({"error": "payment id missing"}), 400

    settings = db.get_payment_settings()
    client = YooKassaPaymentClient(settings)

    # Не доверяем одному телу webhook: подтверждаем актуальный статус платежа через API ЮKassa.
    try:
        status = client.get_payment_status(provider_order_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    order = db.get_order_by_provider_order_id(provider_order_id)
    if not order:
        local_order_id = (obj.get("metadata") or {}).get("local_order_id")
        if local_order_id:
            try:
                order = db.get_order(int(local_order_id))
            except Exception:
                order = None

    if not order:
        return jsonify({"error": "order not found"}), 404

    if status.get("paid"):
        try:
            db.finalize_paid_order(order["id"], provider_order_id=provider_order_id)
        except Exception as reserve_error:
            # Деньги пришли после истечения локальной брони, а товар уже закончился.
            # Для ЮKassa автоматически возвращаем всю сумму, чтобы не оставить оплаченный заказ без товара.
            try:
                refund = client.refund_payment(provider_order_id, order["total"], order_id=order["id"], reason="Автовозврат: товар закончился после истечения брони")
                db.mark_order_refunded(order["id"], provider_order_id, refund.get("refund_id"), refund.get("status"), refund.get("raw_response"), cancel_order=True)
            except Exception as refund_error:
                db.mark_refund_required(order["id"], provider_order_id, f"{reserve_error}; автовозврат не выполнен: {refund_error}")
        emit_all(order.get("branch_id"))
    elif status.get("canceled"):
        db.mark_payment_canceled(order["id"], provider_order_id=provider_order_id)
        emit_all(order.get("branch_id"))

    return jsonify({"success": True, "event": event, "status": status.get("status")})


@app.route("/api/cart-reserve", methods=["POST"])
def cart_reserve():
    data = request.json or {}

    try:
        branch_id = int(data.get("branch_id"))
        expires_at = db.reserve_cart(
            token=get_cart_token(),
            user_id=session.get("user_id"),
            branch_id=branch_id,
            items=data.get("items", [])
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    emit_all(branch_id)

    return jsonify({
        "success": True,
        "expires_at": expires_at
    })


@app.route("/api/cart-clear", methods=["POST"])
def cart_clear():
    db.clear_reservation(get_cart_token())
    emit_all()
    return jsonify({"success": True})


@app.route("/api/my-orders")
def my_orders():
    uid = session.get("user_id")

    if not uid:
        return jsonify([])

    return jsonify(db.get_user_orders(uid))


@app.route("/api/orders/<int:order_id>/qr")
def order_qr(order_id):
    uid = session.get("user_id")

    if not uid:
        return jsonify({"error": "Сначала войдите"}), 401

    order = db.get_order(order_id)

    if not order or order.get("user_id") != uid:
        return jsonify({"error": "Заказ не найден"}), 404

    if not order.get("pickup_code"):
        return jsonify({"error": "QR появится, когда заказ будет готов"}), 400

    try:
        import qrcode
    except Exception:
        return jsonify({"error": "Не установлен пакет qrcode"}), 500

    img = qrcode.make(str(order["pickup_code"]))
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    from flask import send_file
    return send_file(buf, mimetype="image/png")


@app.route("/api/orders/<int:order_id>/rate", methods=["POST"])
def rate_order(order_id):
    uid = session.get("user_id")

    if not uid:
        return jsonify({"error": "Сначала войдите"}), 401

    data = request.json or {}

    try:
        db.rate_order_items(
            order_id=order_id,
            user_id=uid,
            stars=int(data.get("stars", 0)),
            comment=data.get("comment", "")
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"success": True})


@app.route("/api/orders/<int:order_id>/cancel", methods=["POST"])
def cancel_order(order_id):
    uid = session.get("user_id")

    if not uid:
        return jsonify({"error": "Сначала войдите по номеру телефона"}), 401

    try:
        branch_id = db.cancel_order(order_id, uid)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    emit_all(branch_id)

    return jsonify({"success": True})


@app.route("/api/operator/login", methods=["POST"])
def kitchen_login():
    if auth_is_blocked("operator"):
        return jsonify({"success": False, "error": "Слишком много попыток входа. Попробуйте через несколько минут."}), 429

    data = request.json or {}
    kitchen = db.get_kitchen(data.get("login", ""), data.get("password", ""))

    if not kitchen:
        auth_failed("operator")
        return jsonify({"success": False, "error": "Неверный логин или пароль"}), 401

    auth_succeeded("operator")
    session.permanent = True
    session["kitchen_id"] = kitchen["id"]
    session["kitchen_branch_id"] = kitchen["branch_id"]

    return jsonify({"success": True, "kitchen": kitchen})


@app.route("/api/operator/logout", methods=["POST"])
def kitchen_logout():
    session.pop("kitchen_id", None)
    session.pop("kitchen_branch_id", None)

    return jsonify({"success": True})


@app.route("/api/operator/me")
def kitchen_me():
    kid = session.get("kitchen_id")

    if not kid:
        return jsonify({"error": "Не авторизован"}), 401

    return jsonify(db.get_kitchen_by_id(kid))


@app.route("/api/operator/orders")
def kitchen_orders():
    branch_id = session.get("kitchen_branch_id")

    if not branch_id:
        return jsonify({"error": "Не авторизован"}), 401

    return jsonify(db.get_active_orders(branch_id))



@app.route("/api/operator/menu")
def operator_menu():
    branch_id = session.get("kitchen_branch_id")
    if not branch_id:
        return jsonify({"error": "Не авторизован"}), 401
    return jsonify(db.get_operator_menu(branch_id))


@app.route("/api/operator/menu/<int:menu_id>/sold-out", methods=["POST"])
def operator_sold_out(menu_id):
    branch_id = session.get("kitchen_branch_id")
    if not branch_id:
        return jsonify({"error": "Не авторизован"}), 401
    try:
        result = db.operator_mark_sold_out(menu_id, branch_id, session.get("kitchen_id"))
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    emit_all(branch_id)
    return jsonify({"success": True, **result})


@app.route("/api/operator/<int:order_id>/next", methods=["POST"])
def kitchen_next(order_id):
    branch_id = session.get("kitchen_branch_id")

    if not branch_id:
        return jsonify({"error": "Не авторизован"}), 401

    try:
        db.next_order_status(order_id, branch_id, session.get('kitchen_id'))

    except Exception as e:
        return jsonify({"error": str(e)}), 400

    emit_all(branch_id)

    return jsonify({"success": True})


@app.route("/api/operator/find-code", methods=["POST"])
def kitchen_find_code():
    branch_id = session.get("kitchen_branch_id")

    if not branch_id:
        return jsonify({"error": "Не авторизован"}), 401

    code = (request.json or {}).get("code", "").strip()
    order = db.find_ready_order_by_code(branch_id, code)

    if not order:
        previous = db.find_order_by_pickup_code_any(branch_id, code)
        if previous:
            if previous.get("status") == "issued":
                return jsonify({"error": f"Заказ #{previous['id']} уже выдан в {previous.get('issued_at') or 'неизвестное время'}", "already_issued": True}), 409
            return jsonify({"error": f"Заказ #{previous['id']} найден, но его статус: {previous.get('status')}" }), 409
        return jsonify({"error": "Заказ с таким кодом не найден"}), 404

    return jsonify(order)


@app.route("/api/operator/issue-code", methods=["POST"])
def kitchen_issue_code():
    branch_id = session.get("kitchen_branch_id")

    if not branch_id:
        return jsonify({"error": "Не авторизован"}), 401

    code = (request.json or {}).get("code", "").strip()

    try:
        order_id = db.issue_order_by_code(branch_id, code, session.get("kitchen_id"))
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    emit_all(branch_id)

    return jsonify({"success": True, "order_id": order_id})


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    if auth_is_blocked("admin"):
        return jsonify({"success": False, "error": "Слишком много попыток входа. Попробуйте через несколько минут."}), 429

    if secrets.compare_digest(str((request.json or {}).get("password") or ""), str(ADMIN_PASSWORD)):
        auth_succeeded("admin")
        session.permanent = True
        session["admin"] = True
        return jsonify({"success": True})

    auth_failed("admin")
    return jsonify({"success": False})


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin", None)
    return jsonify({"success": True})


def require_admin():
    return bool(session.get("admin"))


@app.route("/api/admin/users")
def admin_users():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    return jsonify(db.get_all_users(request.args.get("search", "")))


@app.route("/api/admin/users/<int:user_id>/orders")
def admin_user_orders(user_id):
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    return jsonify(db.get_user_orders(user_id))


@app.route("/api/admin/orders")
def admin_orders():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    return jsonify(db.get_all_orders(
        status=request.args.get("status", ""),
        branch_id=request.args.get("branch_id", ""),
        search=request.args.get("search", "")
    ))


@app.route("/api/admin/incidents")
def admin_incidents():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401
    status = request.args.get("status", "open")
    db.scan_state_incidents()
    return jsonify(db.get_incidents(status=status, limit=200))


@app.route("/api/admin/incidents/<int:incident_id>/resolve", methods=["POST"])
def admin_resolve_incident(incident_id):
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401
    db.resolve_incident(incident_id)
    return jsonify({"success": True})


@app.route("/api/admin/audit")
def admin_audit():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401
    limit = min(500, max(1, int(request.args.get("limit", 200) or 200)))
    return jsonify(db.get_action_log(limit=limit))


@app.route("/api/admin/health")
def admin_health():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    snapshot = db.get_health_snapshot()
    settings = db.get_payment_settings()
    provider = settings.get("provider") or "demo"
    configured = True
    if settings.get("enabled") == "1" and provider == "yookassa":
        configured = bool(settings.get("yookassa_shop_id") and settings.get("yookassa_secret_key"))
    elif settings.get("enabled") == "1" and provider == "sber":
        configured = bool(settings.get("username") and settings.get("password"))

    backup_info = {"exists": False, "latest": None, "age_minutes": None}
    try:
        backups = sorted(BACKUP_DIR.glob("canteen_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        if backups:
            latest = backups[0]
            backup_info["exists"] = True
            backup_info["latest"] = latest.name
            backup_info["age_minutes"] = round(max(0, (time.time() - latest.stat().st_mtime) / 60), 1)
    except Exception:
        pass

    disk = shutil.disk_usage(BASE_DIR)
    snapshot.update({
        "server_time": datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "db_ok": True,
        "payment_provider": provider,
        "payment_enabled": settings.get("enabled") == "1",
        "payment_configured": configured,
        "backup": backup_info,
        "disk_free_mb": round(disk.free / 1024 / 1024),
        "disk_total_mb": round(disk.total / 1024 / 1024),
    })
    return jsonify(snapshot)


@app.route("/api/admin/analytics")
def admin_analytics():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    return jsonify(db.get_analytics())


@app.route("/api/admin/combos", methods=["GET"])
def admin_combos():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    return jsonify(db.get_all_combos())


@app.route("/api/admin/combos", methods=["POST"])
def admin_add_combo():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    image = save_image(request.files.get("image"))

    raw_items = request.form.get("items", "[]")

    try:
        import json
        items = json.loads(raw_items)
    except Exception:
        items = []

    combo_id = db.add_combo(
        name=request.form.get("name", "").strip(),
        price=int(request.form.get("price", 0) or 0),
        icon="",
        description=request.form.get("description", "").strip(),
        image=image,
        items=items
    )

    emit_all()

    return jsonify({"success": True, "id": combo_id})


@app.route("/api/admin/combos/<int:combo_id>/delete", methods=["DELETE"])
def admin_delete_combo(combo_id):
    if not require_admin():
        return jsonify({"error":"Нет доступа"}), 401

    db.delete_combo(combo_id)
    emit_all()
    return jsonify({"success":True})


@app.route("/api/admin/combos/<int:combo_id>/hide", methods=["POST"])
def admin_hide_combo(combo_id):
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    db.set_combo_available(combo_id, 0)
    emit_all()
    return jsonify({"success": True})


@app.route("/api/admin/combos/<int:combo_id>/restore", methods=["POST"])
def admin_restore_combo(combo_id):
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    db.set_combo_available(combo_id, 1)
    emit_all()
    return jsonify({"success": True})


@app.route("/api/admin/branches", methods=["POST"])
def admin_add_branch():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    data = request.json or {}

    try:
        branch_id = db.add_branch(data.get("name", ""))
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    emit_all()

    return jsonify({"success": True, "id": branch_id})


@app.route("/api/admin/branches/<int:branch_id>/closed", methods=["POST"])
def admin_branch_closed(branch_id):
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    data = request.json or {}

    db.set_branch_closed(
        branch_id=branch_id,
        closed=bool(data.get("closed")),
        reason=data.get("reason", "")
    )

    emit_all()

    return jsonify({"success": True})


@app.route("/api/admin/schedule", methods=["GET"])
def admin_schedule():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    return jsonify(db.get_branch_schedule())


@app.route("/api/admin/schedule/bulk", methods=["POST"])
def admin_update_schedule_bulk():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    data = request.json or {}
    schedules = data.get("schedules", [])

    try:
        db.update_branch_schedule_bulk(schedules)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    emit_all()
    return jsonify({"success": True})


@app.route("/api/admin/schedule", methods=["POST"])
def admin_update_schedule():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    data = request.json or {}

    db.update_branch_schedule(
        branch_id=int(data.get("branch_id")),
        weekday=int(data.get("weekday")),
        open_time=data.get("open_time", "08:00"),
        close_time=data.get("close_time", "20:00"),
        enabled=1 if data.get("enabled") in [True, 1, '1', 'true', 'True'] else 0
    )

    emit_all()
    return jsonify({"success": True})


@app.route("/api/admin/operators", methods=["GET"])
def admin_operators():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    return jsonify(db.get_operators())


@app.route("/api/admin/operators", methods=["POST"])
def admin_add_operator():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    data = request.json or {}

    try:
        operator_id = db.add_operator(
            login=data.get("login", ""),
            password=data.get("password", ""),
            branch_id=data.get("branch_id")
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    emit_all()

    return jsonify({"success": True, "id": operator_id})


@app.route("/api/admin/operators/<int:operator_id>", methods=["POST"])
def admin_update_operator(operator_id):
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    data = request.json or {}

    try:
        db.update_operator(
            operator_id=operator_id,
            password=data.get("password") or None,
            branch_id=data.get("branch_id") or None
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    emit_all()

    return jsonify({"success": True})


@app.route("/api/admin/operators/<int:operator_id>", methods=["DELETE"])
def admin_delete_operator(operator_id):
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    db.delete_operator(operator_id)
    emit_all()

    return jsonify({"success": True})


@app.route("/api/admin/payment-settings", methods=["GET"])
def admin_payment_settings():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    data = db.get_payment_settings()

    # Пароль показываем только маской.
    if data.get("password"):
        data["password_saved"] = True
        data["password"] = ""

    if data.get("onec_token"):
        data["onec_token_saved"] = True
        data["onec_token"] = ""

    if data.get("yookassa_secret_key"):
        data["yookassa_secret_key_saved"] = True
        data["yookassa_secret_key"] = ""

    return jsonify(data)


@app.route("/api/admin/payment-settings", methods=["POST"])
def admin_save_payment_settings():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    data = request.json or {}

    # Если пароль/токен пустые, не затираем сохранённые значения.
    current = db.get_payment_settings()
    if not data.get("password"):
        data["password"] = current.get("password", "")
    if not data.get("onec_token"):
        data["onec_token"] = current.get("onec_token", "")
    if not data.get("yookassa_secret_key"):
        data["yookassa_secret_key"] = current.get("yookassa_secret_key", "")

    db.save_payment_settings(data)
    return jsonify({"success": True})


@app.route("/api/admin/payment-settings/test", methods=["POST"])
def admin_test_payment_settings():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    incoming = request.json or {}
    current = db.get_payment_settings()
    merged = dict(current)
    merged.update({k: v for k, v in incoming.items() if v not in [None, ""]})

    # Пустое секретное поле в форме означает "использовать сохранённое".
    if not incoming.get("yookassa_secret_key"):
        merged["yookassa_secret_key"] = current.get("yookassa_secret_key", "")
    if not incoming.get("password"):
        merged["password"] = current.get("password", "")

    try:
        if (merged.get("provider") or "demo") != "demo":
            merged["enabled"] = "1"
        client = build_payment_client(merged)
        result = client.check_connection()
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/admin/payments/<int:order_id>/refund", methods=["POST"])
def admin_refund_payment(order_id):
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    payment = db.get_refundable_payment(order_id)
    if not payment:
        return jsonify({"error": "Оплаченный платёж для возврата не найден"}), 404

    # Автоматический возврат из админки разрешаем до начала приготовления.
    if payment.get("order_status") != "new":
        return jsonify({"error": "Автовозврат разрешён только пока заказ не принят оператором. После начала приготовления оформите возврат по регламенту."}), 400

    settings = db.get_payment_settings()
    provider = payment.get("provider") or "demo"
    try:
        client = build_payment_client(settings, provider_override=provider)
        result = client.refund_payment(
            payment.get("provider_order_id"),
            payment.get("amount") or payment.get("total"),
            order_id=order_id,
            reason=f"Возврат заказа #{order_id} администратором"
        )
        db.mark_order_refunded(
            order_id,
            payment.get("provider_order_id"),
            result.get("refund_id"),
            result.get("status"),
            result.get("raw_response"),
            cancel_order=True
        )
        order = db.get_order(order_id)
        emit_all(order.get("branch_id") if order else None)
        return jsonify({"success": True, "refund_id": result.get("refund_id"), "status": result.get("status")})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/admin/payments")
def admin_payments():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    return jsonify(db.get_payment_log())


@app.route("/api/admin/logs")
def admin_logs():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    order_id = request.args.get("order_id")
    return jsonify(db.get_action_log(order_id=int(order_id) if order_id else None))


@app.route("/api/admin/menu")
def admin_menu():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    return jsonify(db.get_all_menu_with_stock())


@app.route("/api/admin/menu", methods=["POST"])
def admin_add_menu():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    image = save_image(request.files.get("image"))

    item_id = db.add_menu_item(
        name=request.form.get("name", "").strip(),
        price=int(request.form.get("price", 0) or 0),
        icon="",
        description=request.form.get("description", "").strip(),
        image=image,
        is_special=int(request.form.get("is_special", 0) or 0),
        old_price=int(request.form.get("old_price", 0) or 0),
        discount_price=int(request.form.get("discount_price", 0) or 0),
        writeoff_enabled=int(request.form.get("writeoff_enabled", 0) or 0),
        writeoff_hours=int(request.form.get("writeoff_hours", 0) or 0),
    )

    for branch in db.get_branches():
        qty = int(request.form.get(f"quantity_{branch['id']}", 0) or 0)
        db.set_stock(item_id, branch["id"], qty)

    emit_all()

    return jsonify({"success": True, "id": item_id})


@app.route("/api/admin/menu/<int:item_id>", methods=["PUT"])
def admin_edit_menu(item_id):
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    data = request.json or {}

    db.update_menu_item(
        item_id=item_id,
        name=data.get("name", "").strip(),
        price=int(data.get("price", 0) or 0),
        icon="",
        description=data.get("description", "").strip(),
        available=int(data.get("available", 1) or 0),
        is_special=int(data.get("is_special", 0) or 0),
        old_price=int(data.get("old_price", 0) or 0),
        discount_price=int(data.get("discount_price", 0) or 0),
        writeoff_enabled=int(data.get("writeoff_enabled", 0) or 0),
        writeoff_hours=int(data.get("writeoff_hours", 0) or 0),
    )

    for branch_id, qty in (data.get("stock") or {}).items():
        db.set_stock(item_id, int(branch_id), int(qty or 0))

    emit_all()

    return jsonify({"success": True})


@app.route("/api/admin/menu/<int:item_id>/hide", methods=["POST"])
def admin_hide_menu(item_id):
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    db.set_available(item_id, 0)
    emit_all()

    return jsonify({"success": True})


@app.route("/api/admin/menu/<int:item_id>/restore", methods=["POST"])
def admin_restore_menu(item_id):
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    db.set_available(item_id, 1)
    emit_all()

    return jsonify({"success": True})


@app.route("/api/admin/menu/<int:item_id>/delete", methods=["DELETE"])
def admin_delete_menu(item_id):
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    db.delete_menu_item(item_id)
    emit_all()
    return jsonify({"success": True})


@app.route("/api/admin/menu/<int:item_id>/writeoff", methods=["POST"])
def admin_menu_writeoff(item_id):
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    data = request.json or {}
    db.set_menu_writeoff(item_id, int(data.get("enabled", 0)))
    emit_all()
    return jsonify({"success": True})


@app.route("/api/admin/stock", methods=["POST"])
def admin_stock():
    if not require_admin():
        return jsonify({"error": "Нет доступа"}), 401

    data = request.json or {}

    db.set_stock(
        int(data["menu_id"]),
        int(data["branch_id"]),
        int(data["quantity"])
    )

    emit_all(int(data["branch_id"]))

    return jsonify({"success": True})


@socketio.on("join_branch")
def join_branch_handler(data):
    bid = data.get("branch_id")

    if bid:
        join_room(f"branch_{bid}")



@app.route("/payment-success")
def payment_success():
    return """
    <html><head><meta charset='utf-8'><title>Оплата прошла</title></head>
    <body style='font-family:Arial;padding:40px'><h1>Оплата прошла</h1><p>Статус подтверждается сервером.</p><a href='/'>Вернуться к заказам</a></body></html>
    """


@app.route("/payment-fail")
def payment_fail():
    return """
    <html><head><meta charset='utf-8'><title>Ошибка оплаты</title></head>
    <body style='font-family:Arial;padding:40px'><h1>Оплата не прошла</h1><p>Попробуйте ещё раз или оплатите на кассе.</p><a href='/'>Вернуться на сайт</a></body></html>
    """


if __name__ == "__main__":
    threading.Thread(target=expire_orders_loop, daemon=True).start()

    print("Клиент:  http://127.0.0.1:5000")
    print("Оператор: http://127.0.0.1:5000/operator-login")
    print("Админка:  http://127.0.0.1:5000/admin")

    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("APP_PORT", "5000")),
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True
    )
