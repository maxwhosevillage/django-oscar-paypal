"""
Transaction-level operations: every PayPal call made for an Oscar checkout
goes through here and is recorded on an ``ExpressCheckoutTransaction``.
"""
import json
import logging
import uuid

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _

from paypal.express_checkout.gateway import (
    PaymentProcessor, build_experience_context, build_order_body, build_payer, build_purchase_unit, to_decimal)
from paypal.express_checkout.models import ExpressCheckoutTransaction as Transaction

logger = logging.getLogger('paypal.express_checkout')


def get_intent():
    intent = getattr(settings, 'PAYPAL_ORDER_INTENT', Transaction.CAPTURE)
    if intent not in (Transaction.CAPTURE, Transaction.AUTHORIZE):
        message = _("'%s' is not a valid order intent") % intent
        raise ImproperlyConfigured(message)
    return intent


def get_currency(basket):
    return basket.currency or getattr(settings, 'PAYPAL_CURRENCY', 'EUR')


def create_order(
        basket, order_total, shipping_charge=None, surcharges=None, shipping_address=None, billing_address=None,
        email=None, order_number=None, absolute_uri=None, return_url=None, cancel_url=None, processor=None,
):
    """
    Register an order with PayPal and return the new transaction. The buyer
    approves it with the PayPal JS SDK using ``transaction.order_id``.

    ``order_total`` and ``shipping_charge`` are the incl. tax amounts.
    """
    currency = get_currency(basket)
    intent = get_intent()
    purchase_unit = build_purchase_unit(
        basket, currency, order_total,
        shipping_charge=shipping_charge,
        surcharges=surcharges,
        shipping_address=shipping_address,
        order_number=order_number,
        absolute_uri=absolute_uri,
    )
    body = build_order_body(
        purchase_unit,
        intent=intent,
        experience_context=build_experience_context(shipping_address, return_url, cancel_url),
        payer=build_payer(email, billing_address),
    )
    result = (processor or PaymentProcessor()).create_order(body, request_id=str(uuid.uuid4()))

    txn = Transaction.objects.create(
        order_id=result['id'],
        order_number=order_number or '',
        basket_id=basket.id,
        amount=to_decimal(order_total),
        currency=currency,
        status=result['status'],
        intent=intent,
        address_full_name=shipping_address.name if shipping_address is not None else '',
        address=json.dumps(purchase_unit.get('shipping', {}).get('address', {})),
    )
    logger.info('Basket #%s: created PayPal order %s over %s %s', basket.id, txn.order_id, txn.amount, currency)
    return txn


def _update_payer(txn, result):
    """
    Store payer details from an order/capture response.
    """
    source = result.get('payment_source', {}).get('paypal', {})
    payer = result.get('payer', {})
    txn.payer_id = source.get('account_id') or payer.get('payer_id') or txn.payer_id
    txn.email = source.get('email_address') or payer.get('email_address') or txn.email


def fetch_transaction_details(order_id, processor=None):
    """
    Refresh a transaction with the current order state from PayPal.
    """
    txn = Transaction.objects.get(order_id=order_id)
    result = (processor or PaymentProcessor()).get_order(order_id)
    _update_payer(txn, result)
    txn.status = result['status']
    txn.save()
    return txn


def get_approved_amount(order_id, processor=None):
    """
    Return ``(amount, currency)`` of the order as PayPal knows it.
    """
    result = (processor or PaymentProcessor()).get_order(order_id)
    amount = result['purchase_units'][0]['amount']
    return to_decimal(amount['value']), amount['currency_code'], result['status']


def complete_payment(txn, processor=None):
    """
    Take the money for an approved order: capture it (intent CAPTURE) or
    authorize it for a later capture (intent AUTHORIZE).

    Raises ``PayPalError`` if PayPal refuses, e.g. with issue
    ``INSTRUMENT_DECLINED`` when the buyer has to pick another funding source.
    """
    processor = processor or PaymentProcessor()
    if txn.is_authorization:
        result = processor.authorize_order(txn.order_id)
        payment = result['purchase_units'][0]['payments']['authorizations'][0]
        txn.authorization_id = payment['id']
    else:
        result = processor.capture_order(txn.order_id)
        payment = result['purchase_units'][0]['payments']['captures'][0]
        txn.capture_id = payment['id']
        txn.capture_status = payment['status']

    _update_payer(txn, result)
    txn.status = result['status']
    txn.save()
    logger.info(
        'PayPal order %s (order #%s): %s %s', txn.order_id, txn.order_number, txn.intent.lower(), payment['status'])
    return txn


def capture_authorization(txn, processor=None):
    result = (processor or PaymentProcessor()).capture_authorization(txn.authorization_id, txn.order_number)
    txn.capture_id = result['id']
    txn.capture_status = result['status']
    txn.status = Transaction.COMPLETED
    txn.save()
    return txn


def void_authorization(txn, processor=None):
    (processor or PaymentProcessor()).void_authorization(txn.authorization_id)
    txn.status = Transaction.VOIDED
    txn.save()
    return txn


def refund(txn, amount=None, note_to_payer=None, processor=None):
    """
    Refund (part of) a captured payment. Returns PayPal's refund resource.
    """
    result = (processor or PaymentProcessor()).refund_capture(
        txn.capture_id, amount=amount, currency=txn.currency, note_to_payer=note_to_payer,
        request_id=str(uuid.uuid4()))
    txn.refund_id = result['id']
    txn.save()
    logger.info('PayPal capture %s (order #%s): refunded %s', txn.capture_id, txn.order_number, amount or 'all')
    return result


def add_tracking(txn, tracking_number, carrier, carrier_name_other=None, notify_payer=False, processor=None):
    """
    Send the shipment's tracking number to PayPal.
    """
    (processor or PaymentProcessor()).add_tracking(
        txn.order_id, txn.capture_id, tracking_number, carrier,
        carrier_name_other=carrier_name_other, notify_payer=notify_payer)
    txn.tracking_number = tracking_number
    txn.carrier = carrier_name_other or carrier
    txn.save()
    return txn
