"""Web layer: routes only.

Stripe logic lives in `payments`, persistence and state in `orders`, webhook
handling in `webhooks`. The webhook is the sole writer of order state; /success
only reads it (with a display-only PaymentIntent retrieve), never writes.
"""
import logging
import os
import uuid

from flask import Flask, jsonify, render_template, request

import orders
import payments
import webhooks
from config import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = Flask(
    __name__,
    static_url_path="",
    template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "views"),
    static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "public"),
)

orders.init_db()


def format_money(amount: int, currency: str = payments.CURRENCY) -> str:
    """cents -> display string. Single currency (USD) for now."""
    return f"${amount / 100:,.2f}"


# Display-only fallback when /success is hit for a PaymentIntent with no local
# order row. Never used to write state.
_DISPLAY_STATE = {
    "succeeded": orders.PAID,
    "processing": orders.PROCESSING,
    "requires_payment_method": orders.REQUIRES_PAYMENT,
    "requires_action": orders.REQUIRES_PAYMENT,
    "requires_confirmation": orders.REQUIRES_PAYMENT,
    "canceled": orders.FAILED,
}


@app.route("/", methods=["GET"])
def index():
    books = list(payments.CATALOG.values())
    return render_template("index.html", books=books, format_money=format_money)


@app.route("/checkout", methods=["GET"])
def checkout():
    item = request.args.get("item")
    try:
        book = payments.get_book(item)
    except payments.UnknownItem:
        return render_template("checkout.html", error="No item selected"), 400
    return render_template(
        "checkout.html",
        item=book.id,
        title=book.title,
        amount=book.amount,
        amount_display=format_money(book.amount),
        publishable_key=config.stripe_publishable_key,
        domain=config.domain,
        error=None,
    )


@app.route("/create-payment-intent", methods=["POST"])
def create_payment_intent():
    """Create (or reuse) the PaymentIntent for one logical order.

    Amount is derived server-side from the catalog; the client sends only the
    item id and a stable `order_ref`. Reusing an existing order's intent avoids
    minting a duplicate on retry/refresh.
    """
    data = request.get_json(silent=True) or {}
    item = data.get("item")
    order_ref = data.get("order_ref")
    if not item or not order_ref:
        return jsonify(error="item and order_ref are required"), 400

    existing = orders.get_order_by_ref(order_ref)
    if existing is not None:
        intent = payments.retrieve_intent(existing.pi_id)
        return jsonify(clientSecret=intent.client_secret, pi_id=intent.id)

    try:
        intent = payments.create_intent(order_ref, item)
    except payments.UnknownItem:
        return jsonify(error="Unknown item"), 400

    book = payments.get_book(item)
    orders.create_order(
        pi_id=intent.id,
        order_ref=order_ref,
        item=book.id,
        title=book.title,
        amount=book.amount,
        currency=payments.CURRENCY,
    )
    return jsonify(clientSecret=intent.client_secret, pi_id=intent.id)


@app.route("/webhook", methods=["POST"])
def webhook():
    """Stripe event ingress: the only place order state is written.

    Reads the raw body (required for signature verification) and returns
    construct_and_dispatch's (status, message) verbatim.
    """
    request_id = uuid.uuid4().hex[:12]
    status, message = webhooks.construct_and_dispatch(
        payload=request.get_data(),
        sig_header=request.headers.get("Stripe-Signature", ""),
        webhook_secret=config.stripe_webhook_secret,
        request_id=request_id,
    )
    return message, status


@app.route("/success", methods=["GET"])
def success():
    """Confirmation page, read-only.

    Fulfillment truth is the order row, advanced solely by the webhook; the live
    PaymentIntent is retrieved only for optimistic display when the redirect
    beats the webhook. Display may lead the database; this never writes it.
    """
    pi_id = request.args.get("payment_intent")
    if not pi_id:
        return render_template("success.html", error="Missing payment reference"), 400

    order = orders.get_order_by_pi(pi_id)      # server-owned fulfillment truth
    intent = payments.retrieve_intent(pi_id)   # display fallback only; never persisted

    paid = intent.status == "succeeded" or (order is not None and order.state == orders.PAID)
    amount = order.amount if order else intent.amount
    title = order.title if order else None
    state = order.state if order else _DISPLAY_STATE.get(intent.status, intent.status)

    return render_template(
        "success.html",
        error=None,
        pi_id=pi_id,
        title=title,
        amount_display=format_money(amount),
        paid=paid,
        state=state,
    )


if __name__ == "__main__":
    # 4242: Stripe's sample-app convention; also sidesteps macOS :5000 (AirPlay).
    app.run(port=4242, host="127.0.0.1", debug=True)
