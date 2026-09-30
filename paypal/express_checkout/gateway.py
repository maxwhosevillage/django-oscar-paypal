"""
Builds PayPal Orders v2 payloads from Oscar objects and wraps the API calls.

API reference: https://developer.paypal.com/docs/api/orders/v2/
"""
from decimal import ROUND_HALF_UP
from decimal import Decimal as D

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.template.defaultfilters import striptags, truncatechars
from django.utils.translation import gettext_lazy as _

from paypal.express_checkout.client import PayPalClient

INTENT_AUTHORIZE = 'AUTHORIZE'
INTENT_CAPTURE = 'CAPTURE'

LANDING_PAGE_LOGIN = 'LOGIN'
LANDING_PAGE_GUEST_CHECKOUT = 'GUEST_CHECKOUT'
LANDING_PAGE_NO_PREFERENCE = 'NO_PREFERENCE'
# Name used by the legacy NVP API / older settings
LANDING_PAGE_BILLING = 'BILLING'

SHIPPING_SET_PROVIDED_ADDRESS = 'SET_PROVIDED_ADDRESS'
SHIPPING_NO_SHIPPING = 'NO_SHIPPING'

USER_ACTION_CONTINUE = 'CONTINUE'
USER_ACTION_PAY_NOW = 'PAY_NOW'

# PayPal field length limits
MAX_TEXT = 127
MAX_NAME = 300


def format_description(description):
    return truncatechars(striptags(description), MAX_TEXT) if description else ''


def to_decimal(amount):
    return D(amount).quantize(D('0.01'), rounding=ROUND_HALF_UP)


def format_amount(amount):
    return str(to_decimal(amount))


def money(amount, currency):
    return {'currency_code': currency, 'value': format_amount(amount)}


def get_landing_page():
    landing_page = getattr(settings, 'PAYPAL_LANDING_PAGE', LANDING_PAGE_NO_PREFERENCE)
    if landing_page == LANDING_PAGE_BILLING:
        landing_page = LANDING_PAGE_GUEST_CHECKOUT
    if landing_page not in (LANDING_PAGE_LOGIN, LANDING_PAGE_GUEST_CHECKOUT, LANDING_PAGE_NO_PREFERENCE):
        message = _("'%s' is not a valid landing page") % landing_page
        raise ImproperlyConfigured(message)
    return landing_page


def build_address(address):
    """
    Oscar address -> PayPal ``address_portable``.
    """
    line2 = ', '.join(part for part in (address.line2, address.line3) if part)
    data = {
        'address_line_1': truncatechars(address.line1, MAX_NAME),
        'admin_area_2': truncatechars(address.line4, 120),
        'postal_code': address.postcode,
        'country_code': address.country.iso_3166_1_a2,
    }
    if line2:
        data['address_line_2'] = truncatechars(line2, MAX_NAME)
    if address.state:
        data['admin_area_1'] = truncatechars(address.state, MAX_NAME)
    return data


def build_items(basket, currency, absolute_uri=None):
    """
    One PayPal item per basket line, priced at the undiscounted unit price
    incl. tax (discounts go into the breakdown). ``absolute_uri`` turns
    relative URLs into absolute ones; when given, product links and images
    are sent along and shown to the buyer in their PayPal account.
    """
    items = []
    for line in basket.all_lines():
        product = line.product
        item = {
            'name': truncatechars(product.get_title(), MAX_TEXT),
            'quantity': str(line.quantity),
            'unit_amount': money(line.unit_price_incl_tax, currency),
            'category': 'PHYSICAL_GOODS' if product.is_shipping_required else 'DIGITAL_GOODS',
        }
        sku = line.stockrecord.partner_sku if line.stockrecord else product.upc
        if sku:
            item['sku'] = truncatechars(sku, MAX_TEXT)
        description = format_description(product.description)
        if description:
            item['description'] = description
        if absolute_uri is not None:
            url = absolute_uri(product.get_absolute_url())
            if url.startswith('https://'):
                item['url'] = url
            image = product.primary_image()
            original = getattr(image, 'original', None)
            if original:
                image_url = absolute_uri(original.url)
                if image_url.startswith('https://'):
                    item['image_url'] = image_url
        items.append(item)
    return items


def build_purchase_unit(
        basket, currency, order_total, shipping_charge=None, surcharges=None, shipping_address=None,
        order_number=None, absolute_uri=None,
):
    """
    Build the single purchase unit for a basket.

    PayPal validates that ``item_total + shipping + handling - discount``
    equals ``amount``. Oscar already applied offers and vouchers to the totals,
    so the discount is derived from the difference. Should rounding ever make
    that impossible, items and breakdown are left out rather than failing the
    checkout.
    """
    order_total = to_decimal(order_total)
    purchase_unit = {
        'amount': money(order_total, currency),
    }

    items = build_items(basket, currency, absolute_uri=absolute_uri)
    item_total = sum((to_decimal(item['unit_amount']['value']) * int(item['quantity']) for item in items), D('0.00'))
    shipping = to_decimal(shipping_charge) if shipping_charge is not None else D('0.00')
    handling = sum((to_decimal(s.price.incl_tax) for s in surcharges or []), D('0.00'))
    discount = item_total + shipping + handling - order_total

    if items and discount >= 0:
        breakdown = {'item_total': money(item_total, currency)}
        if shipping_charge is not None:
            breakdown['shipping'] = money(shipping, currency)
        if handling:
            breakdown['handling'] = money(handling, currency)
        if discount:
            breakdown['discount'] = money(discount, currency)
        purchase_unit['amount']['breakdown'] = breakdown
        purchase_unit['items'] = items

    if order_number:
        purchase_unit['invoice_id'] = str(order_number)
        purchase_unit['reference_id'] = str(order_number)
    purchase_unit['custom_id'] = str(basket.id)

    description = getattr(settings, 'PAYPAL_ORDER_DESCRIPTION', None)
    if description:
        purchase_unit['description'] = truncatechars(
            str(description).format(order_number=order_number or ''), MAX_TEXT)
    soft_descriptor = getattr(settings, 'PAYPAL_SOFT_DESCRIPTOR', None)
    if soft_descriptor:
        purchase_unit['soft_descriptor'] = soft_descriptor[:22]

    if shipping_address is not None:
        purchase_unit['shipping'] = {
            'type': 'SHIPPING',
            'name': {'full_name': truncatechars(shipping_address.name, MAX_NAME)},
            'address': build_address(shipping_address),
        }
    return purchase_unit


def build_experience_context(shipping_address=None, return_url=None, cancel_url=None):
    context = {
        'landing_page': get_landing_page(),
        # The address was collected (and shipping calculated for it) in the shop,
        # so the buyer must not change it on PayPal.
        'shipping_preference': SHIPPING_SET_PROVIDED_ADDRESS if shipping_address is not None else SHIPPING_NO_SHIPPING,
        # The order is placed right after approval, there is no further review step
        'user_action': USER_ACTION_PAY_NOW,
        # Disallow payment methods that settle days later (e.g. eCheck)
        'payment_method_preference': getattr(
            settings, 'PAYPAL_PAYMENT_METHOD_PREFERENCE', 'IMMEDIATE_PAYMENT_REQUIRED'),
    }
    brand_name = getattr(settings, 'PAYPAL_BRAND_NAME', None)
    if brand_name:
        context['brand_name'] = truncatechars(brand_name, MAX_TEXT)
    locale = getattr(settings, 'PAYPAL_LOCALE', None)
    if locale:
        context['locale'] = locale
    if return_url:
        context['return_url'] = return_url
    if cancel_url:
        context['cancel_url'] = cancel_url
    return context


def build_payer(email=None, billing_address=None):
    """
    Prefill PayPal's login/guest form with what the shop already knows.
    """
    payer = {}
    if email:
        payer['email_address'] = email
    if billing_address is not None:
        name = {}
        if billing_address.first_name:
            name['given_name'] = truncatechars(billing_address.first_name, 140)
        if billing_address.last_name:
            name['surname'] = truncatechars(billing_address.last_name, 140)
        if name:
            payer['name'] = name
        payer['address'] = build_address(billing_address)
    return payer


def build_order_body(purchase_unit, intent, experience_context, payer=None):
    paypal_source = {'experience_context': experience_context}
    paypal_source.update(payer or {})
    return {
        'intent': intent,
        'purchase_units': [purchase_unit],
        'payment_source': {'paypal': paypal_source},
    }


class PaymentProcessor:

    def __init__(self, client=None):
        self.client = client or PayPalClient.from_settings()

    def create_order(self, body, request_id=None):
        return self.client.post('/v2/checkout/orders', json=body, request_id=request_id)

    def get_order(self, order_id):
        return self.client.get(f'/v2/checkout/orders/{order_id}')

    def capture_order(self, order_id, request_id=None):
        return self.client.post(
            f'/v2/checkout/orders/{order_id}/capture', request_id=request_id or f'capture-{order_id}')

    def authorize_order(self, order_id, request_id=None):
        return self.client.post(
            f'/v2/checkout/orders/{order_id}/authorize', request_id=request_id or f'authorize-{order_id}')

    def capture_authorization(self, authorization_id, invoice_id=None, request_id=None):
        body = {'final_capture': True}
        if invoice_id:
            body['invoice_id'] = str(invoice_id)
        return self.client.post(
            f'/v2/payments/authorizations/{authorization_id}/capture', json=body,
            request_id=request_id or f'capture-{authorization_id}')

    def void_authorization(self, authorization_id):
        return self.client.post(f'/v2/payments/authorizations/{authorization_id}/void')

    def refund_capture(self, capture_id, amount=None, currency=None, note_to_payer=None, request_id=None):
        """
        Refund a capture. Without ``amount`` the remaining captured amount is
        refunded.
        """
        body = {}
        if amount is not None:
            body['amount'] = money(amount, currency)
        if note_to_payer:
            body['note_to_payer'] = truncatechars(note_to_payer, 255)
        return self.client.post(f'/v2/payments/captures/{capture_id}/refund', json=body, request_id=request_id)

    def add_tracking(self, order_id, capture_id, tracking_number, carrier, carrier_name_other=None,
                     notify_payer=False):
        """
        Attach shipment tracking to a captured order. Buyers see it in PayPal
        and it counts as proof of shipment for seller protection.
        """
        body = {
            'capture_id': capture_id,
            'tracking_number': tracking_number,
            'carrier': carrier,
            'notify_payer': notify_payer,
        }
        if carrier_name_other:
            body['carrier_name_other'] = carrier_name_other
        return self.client.post(
            f'/v2/checkout/orders/{order_id}/track', json=body,
            request_id=f'track-{order_id}-{tracking_number}')
