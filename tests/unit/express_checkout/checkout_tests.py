"""
Tests for the Orders v2 checkout. PayPal is mocked at the HTTP level so the
client, payload building, facade and views run unchanged.
"""
import json
from decimal import Decimal as D

import pytest
import responses
from django.core.cache import cache
from django.template import Context, Template
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from oscar.apps.basket.models import Basket
from oscar.apps.checkout.utils import CheckoutSessionData
from oscar.apps.order.models import Order
from oscar.apps.payment.models import Source
from oscar.core.loading import get_model
from oscar.test.factories import create_product

from paypal.express_checkout import facade, gateway
from paypal.express_checkout.client import SANDBOX_BASE_URL, PayPalClient, PayPalError
from paypal.express_checkout.models import ExpressCheckoutTransaction as Transaction

Country = get_model('address', 'Country')

API = SANDBOX_BASE_URL
ORDER_ID = '5O190127TN364715T'
CAPTURE_ID = '3C679366HH908993F'

PAYPAL_SETTINGS = dict(
    PAYPAL_CLIENT_ID='client-id',
    PAYPAL_CLIENT_SECRET='client-secret',
    PAYPAL_SANDBOX_MODE=True,
    PAYPAL_BRAND_NAME='Herrenmode',
    PAYPAL_LOCALE='de-DE',
)


def mock_token(rsps):
    rsps.add(responses.POST, f'{API}/v1/oauth2/token', json={'access_token': 'token-1', 'expires_in': 32400})


def order_result(status='CREATED', amount='24.94'):
    return {
        'id': ORDER_ID,
        'status': status,
        'purchase_units': [{'amount': {'currency_code': 'GBP', 'value': amount}}],
        'payment_source': {'paypal': {'email_address': 'buyer@example.com', 'account_id': 'PAYERID123'}},
    }


def capture_result(capture_status='COMPLETED'):
    return {
        'id': ORDER_ID,
        'status': 'COMPLETED',
        'payment_source': {'paypal': {'email_address': 'buyer@example.com', 'account_id': 'PAYERID123'}},
        'purchase_units': [{'payments': {'captures': [{'id': CAPTURE_ID, 'status': capture_status}]}}],
    }


def last_json(rsps, path):
    calls = [c for c in rsps.calls if c.request.url.endswith(path)]
    return json.loads(calls[-1].request.body)


class CheckoutMixin:

    def setUp(self):
        super().setUp()
        cache.clear()
        self.country = Country.objects.create(
            iso_3166_1_a2='GB', iso_3166_1_a3='GBR', iso_3166_1_numeric='826', printable_name='United Kingdom',
            name='United Kingdom', is_shipping_country=True)
        self.product = create_product(price=D('19.99'), num_in_stock=10, title='Socke <b>blau</b>')
        self.client.post(reverse('basket:add', kwargs={'pk': self.product.pk}), {'quantity': 1})
        self.basket = Basket.objects.get()

        session = self.client.session
        session[CheckoutSessionData.SESSION_KEY] = {
            'guest': {'email': 'guest@example.com'},
            'shipping': {
                'method_code': 'standard',
                'new_address_fields': {
                    'first_name': 'Sherlock', 'last_name': 'Holmes', 'line1': '221B Baker Street',
                    'line4': 'London', 'postcode': 'NW1 6XE', 'country_id': 'GB',
                },
            },
            'billing': {'billing_address_same_as_shipping': True},
        }
        session.save()

    def create_order(self, rsps):
        mock_token(rsps)
        rsps.add(responses.POST, f'{API}/v2/checkout/orders', json=order_result())
        response = self.client.post(reverse('express-checkout-create-order'), secure=True)
        assert response.status_code == 200, response.content
        return response.json()


@override_settings(**PAYPAL_SETTINGS)
class CreateOrderTests(CheckoutMixin, TestCase):

    @responses.activate
    def test_creates_order_with_items_shipping_and_order_number(self):
        data = self.create_order(responses)
        assert data == {'id': ORDER_ID}

        body = last_json(responses, '/v2/checkout/orders')
        unit = body['purchase_units'][0]
        assert body['intent'] == 'CAPTURE'
        assert unit['amount'] == {
            'currency_code': 'GBP', 'value': '24.94',
            'breakdown': {
                'item_total': {'currency_code': 'GBP', 'value': '19.99'},
                'shipping': {'currency_code': 'GBP', 'value': '4.95'},
            },
        }
        assert unit['items'][0]['name'] == self.product.get_title()
        assert unit['items'][0]['quantity'] == '1'
        assert unit['items'][0]['category'] == 'PHYSICAL_GOODS'
        assert unit['items'][0]['url'] == f'https://testserver{self.product.get_absolute_url()}'
        order_number = str(100000 + self.basket.id)
        assert unit['invoice_id'] == order_number
        assert unit['custom_id'] == str(self.basket.id)
        assert unit['shipping'] == {
            'type': 'SHIPPING',
            'name': {'full_name': 'Sherlock Holmes'},
            'address': {
                'address_line_1': '221B Baker Street', 'admin_area_2': 'London', 'postal_code': 'NW1 6XE',
                'country_code': 'GB',
            },
        }

        paypal_source = body['payment_source']['paypal']
        assert paypal_source['email_address'] == 'guest@example.com'
        assert paypal_source['name'] == {'given_name': 'Sherlock', 'surname': 'Holmes'}
        context = paypal_source['experience_context']
        assert context['shipping_preference'] == 'SET_PROVIDED_ADDRESS'
        assert context['user_action'] == 'PAY_NOW'
        assert context['brand_name'] == 'Herrenmode'
        assert context['locale'] == 'de-DE'
        assert context['payment_method_preference'] == 'IMMEDIATE_PAYMENT_REQUIRED'

        request = [c for c in responses.calls if c.request.url.endswith('/v2/checkout/orders')][0].request
        assert request.headers['Authorization'] == 'Bearer token-1'
        assert request.headers['PayPal-Request-Id']

        txn = Transaction.objects.get()
        assert txn.order_id == ORDER_ID
        assert txn.order_number == order_number
        assert txn.basket_id == self.basket.id
        assert txn.amount == D('24.94')
        assert txn.address_full_name == 'Sherlock Holmes'

    @responses.activate
    def test_paypal_error_returns_json_error(self):
        mock_token(responses)
        responses.add(responses.POST, f'{API}/v2/checkout/orders', status=500,
                      json={'name': 'INTERNAL_SERVER_ERROR', 'message': 'boom', 'debug_id': 'abc'})
        response = self.client.post(reverse('express-checkout-create-order'))
        assert response.status_code == 502
        assert response.json()['error']
        assert not Transaction.objects.exists()

    def test_missing_shipping_method_redirects(self):
        session = self.client.session
        del session[CheckoutSessionData.SESSION_KEY]['shipping']['method_code']
        session.save()
        response = self.client.post(reverse('express-checkout-create-order'))
        assert response.json() == {'redirect': reverse('checkout:shipping-method')}

    def test_get_not_allowed(self):
        assert self.client.get(reverse('express-checkout-create-order')).status_code == 405


@override_settings(**PAYPAL_SETTINGS)
class CaptureOrderTests(CheckoutMixin, TestCase):

    def capture(self, order_id=ORDER_ID):
        return self.client.post(reverse('express-checkout-capture-order'), {'order_id': order_id})

    @responses.activate
    def test_places_order_after_capture(self):
        self.create_order(responses)
        responses.add(responses.POST, f'{API}/v2/checkout/orders/{ORDER_ID}/capture', json=capture_result())

        response = self.capture()
        assert response.json() == {'redirect': reverse('checkout:thank-you')}

        order = Order.objects.get()
        assert order.number == str(100000 + self.basket.id)
        assert order.total_incl_tax == D('24.94')
        assert order.guest_email == 'guest@example.com'
        assert order.shipping_address.line1 == '221B Baker Street'

        source = Source.objects.get(order=order)
        assert source.source_type.name == 'PayPal'
        assert source.amount_debited == D('24.94')
        assert source.reference == ORDER_ID
        event = order.payment_events.get()
        assert event.event_type.name == 'Settled'
        assert event.reference == CAPTURE_ID

        txn = Transaction.objects.get()
        assert txn.capture_id == CAPTURE_ID
        assert txn.capture_status == 'COMPLETED'
        assert txn.status == 'COMPLETED'
        assert txn.payer_id == 'PAYERID123'
        assert txn.email == 'buyer@example.com'

        capture_call = [c for c in responses.calls if c.request.url.endswith('/capture')][0]
        assert capture_call.request.headers['PayPal-Request-Id'] == f'capture-{ORDER_ID}'

    @responses.activate
    def test_order_from_other_session_is_rejected(self):
        self.create_order(responses)
        response = self.capture(order_id='SOMEONEELSE')
        assert response.status_code == 400
        assert not [c for c in responses.calls if c.request.url.endswith('/capture')]
        assert not Order.objects.exists()

    @responses.activate
    def test_changed_basket_is_not_captured(self):
        self.create_order(responses)
        # Buyer adds another item in a second tab while the popup is open
        self.client.post(reverse('basket:add', kwargs={'pk': self.product.pk}), {'quantity': 1})

        response = self.capture()
        assert response.status_code == 400
        assert not [c for c in responses.calls if c.request.url.endswith('/capture')]
        assert not Order.objects.exists()

    @responses.activate
    def test_declined_instrument_asks_js_to_restart(self):
        self.create_order(responses)
        responses.add(responses.POST, f'{API}/v2/checkout/orders/{ORDER_ID}/capture', status=422, json={
            'name': 'UNPROCESSABLE_ENTITY', 'message': 'declined', 'debug_id': 'dbg',
            'details': [{'issue': 'INSTRUMENT_DECLINED'}],
        })

        response = self.capture()
        assert response.status_code == 400
        assert response.json()['restart'] is True
        assert not Order.objects.exists()
        # Basket is usable again for the next attempt
        self.basket.refresh_from_db()
        assert self.basket.status == Basket.OPEN

    @responses.activate
    def test_declined_capture_places_no_order(self):
        self.create_order(responses)
        responses.add(responses.POST, f'{API}/v2/checkout/orders/{ORDER_ID}/capture',
                      json=capture_result(capture_status='DECLINED'))

        response = self.capture()
        assert response.status_code == 400
        assert response.json()['restart'] is False
        assert not Order.objects.exists()

    @responses.activate
    def test_pending_capture_places_order(self):
        self.create_order(responses)
        responses.add(responses.POST, f'{API}/v2/checkout/orders/{ORDER_ID}/capture',
                      json=capture_result(capture_status='PENDING'))

        assert 'redirect' in self.capture().json()
        order = Order.objects.get()
        assert order.payment_events.get().event_type.name == 'Pending'

    @responses.activate
    @override_settings(PAYPAL_ORDER_INTENT='AUTHORIZE')
    def test_authorize_intent(self):
        self.create_order(responses)
        responses.add(responses.POST, f'{API}/v2/checkout/orders/{ORDER_ID}/authorize', json={
            'id': ORDER_ID, 'status': 'COMPLETED',
            'purchase_units': [{'payments': {'authorizations': [{'id': 'AUTH1', 'status': 'CREATED'}]}}],
        })

        assert 'redirect' in self.capture().json()
        order = Order.objects.get()
        assert order.payment_events.get().event_type.name == 'Authorised'
        assert Source.objects.get(order=order).amount_debited == 0
        assert Transaction.objects.get().authorization_id == 'AUTH1'


class PurchaseUnitTests(TestCase):

    def setUp(self):
        self.country = Country.objects.create(
            iso_3166_1_a2='DE', iso_3166_1_a3='DEU', iso_3166_1_numeric='276', printable_name='Deutschland',
            name='Deutschland', is_shipping_country=True)
        self.basket = Basket.objects.create()
        from oscar.apps.partner.strategy import Default
        self.basket.strategy = Default()
        self.basket.add_product(create_product(price=D('10.00'), num_in_stock=10), quantity=3)

    def test_discount_is_derived_from_totals(self):
        unit = gateway.build_purchase_unit(self.basket, 'EUR', D('27.95'), shipping_charge=D('4.95'))
        assert unit['amount']['breakdown'] == {
            'item_total': {'currency_code': 'EUR', 'value': '30.00'},
            'shipping': {'currency_code': 'EUR', 'value': '4.95'},
            'discount': {'currency_code': 'EUR', 'value': '7.00'},
        }

    def test_inconsistent_totals_leave_out_items(self):
        unit = gateway.build_purchase_unit(self.basket, 'EUR', D('30.01'))
        assert unit['amount'] == {'currency_code': 'EUR', 'value': '30.01'}
        assert 'items' not in unit

    def test_surcharges_become_handling(self):
        class Surcharge:
            class price:
                incl_tax = D('2.00')

        unit = gateway.build_purchase_unit(self.basket, 'EUR', D('32.00'), surcharges=[Surcharge()])
        assert unit['amount']['breakdown']['handling'] == {'currency_code': 'EUR', 'value': '2.00'}

    def test_only_image_urls_paypal_accepts_are_sent(self):
        assert gateway.IMAGE_URL_RE.fullmatch('https://www.example.com/media/products/181/14416_6370_04.jpg')
        # Rejected by PayPal's schema: port, percent-encoding, no image extension
        assert not gateway.IMAGE_URL_RE.fullmatch('https://localhost:8012/media/a.jpg')
        assert not gateway.IMAGE_URL_RE.fullmatch('https://www.example.com/media/1100%20C187_10_0.jpg')
        assert not gateway.IMAGE_URL_RE.fullmatch('https://www.example.com/media/a.webp')
        assert not gateway.IMAGE_URL_RE.fullmatch('http://www.example.com/media/a.jpg')

    def test_long_values_are_truncated(self):
        from oscar.apps.order.models import ShippingAddress
        address = ShippingAddress(first_name='A' * 200, last_name='B' * 200, line1='Straße 1', line2='Hinterhaus',
                                  line3='3. OG', line4='Köln', postcode='50667', country=self.country)
        unit = gateway.build_purchase_unit(self.basket, 'EUR', D('30.00'), shipping_address=address)
        assert len(unit['shipping']['name']['full_name']) == 300
        assert unit['shipping']['address']['address_line_2'] == 'Hinterhaus, 3. OG'
        assert unit['shipping']['address']['country_code'] == 'DE'


@override_settings(**PAYPAL_SETTINGS)
class ClientTests(TestCase):

    def setUp(self):
        cache.clear()

    @responses.activate
    def test_token_is_cached(self):
        mock_token(responses)
        responses.add(responses.GET, f'{API}/v2/checkout/orders/{ORDER_ID}', json=order_result())
        client = PayPalClient.from_settings()
        client.get(f'/v2/checkout/orders/{ORDER_ID}')
        client.get(f'/v2/checkout/orders/{ORDER_ID}')
        assert len([c for c in responses.calls if c.request.url.endswith('/oauth2/token')]) == 1

    @responses.activate
    def test_expired_token_is_refreshed_once(self):
        mock_token(responses)
        responses.add(responses.GET, f'{API}/v2/checkout/orders/{ORDER_ID}', status=401, json={})
        responses.add(responses.GET, f'{API}/v2/checkout/orders/{ORDER_ID}', json=order_result())
        assert PayPalClient.from_settings().get(f'/v2/checkout/orders/{ORDER_ID}')['id'] == ORDER_ID
        assert len([c for c in responses.calls if c.request.url.endswith('/oauth2/token')]) == 2

    @responses.activate
    def test_error_details(self):
        mock_token(responses)
        responses.add(responses.POST, f'{API}/v2/checkout/orders', status=422, json={
            'name': 'UNPROCESSABLE_ENTITY', 'message': 'The requested action could not be performed',
            'debug_id': 'f00',
            'details': [{'field': '/purchase_units/0/amount/value', 'issue': 'AMOUNT_MISMATCH'}],
        })
        with pytest.raises(PayPalError) as exc:
            PayPalClient.from_settings().post('/v2/checkout/orders', json={})
        assert exc.value.issue == 'AMOUNT_MISMATCH'
        assert exc.value.debug_id == 'f00'
        assert exc.value.status_code == 422

    @override_settings(PAYPAL_CLIENT_ID='')
    def test_missing_credentials(self):
        from django.core.exceptions import ImproperlyConfigured
        with pytest.raises(ImproperlyConfigured):
            PayPalClient.from_settings()


@override_settings(**PAYPAL_SETTINGS)
class AfterSaleTests(TestCase):

    def setUp(self):
        cache.clear()
        self.txn = Transaction.objects.create(
            order_id=ORDER_ID, capture_id=CAPTURE_ID, amount=D('24.94'), currency='EUR',
            status='COMPLETED', intent='CAPTURE', order_number='100001')

    @responses.activate
    def test_partial_refund(self):
        mock_token(responses)
        responses.add(responses.POST, f'{API}/v2/payments/captures/{CAPTURE_ID}/refund',
                      json={'id': 'REFUND1', 'status': 'COMPLETED'})
        facade.refund(self.txn, amount=D('10'), note_to_payer='Retoure')
        assert last_json(responses, '/refund') == {
            'amount': {'currency_code': 'EUR', 'value': '10.00'}, 'note_to_payer': 'Retoure'}
        assert Transaction.objects.get().refund_id == 'REFUND1'

    @responses.activate
    def test_refund_uses_given_request_id(self):
        mock_token(responses)
        responses.add(responses.POST, f'{API}/v2/payments/captures/{CAPTURE_ID}/refund',
                      json={'id': 'REFUND1', 'status': 'COMPLETED'})
        facade.refund(self.txn, amount=D('10'), request_id='refund-100001-line-7')
        call = [c for c in responses.calls if c.request.url.endswith('/refund')][0]
        assert call.request.headers['PayPal-Request-Id'] == 'refund-100001-line-7'

    @responses.activate
    def test_partial_capture_of_authorization(self):
        mock_token(responses)
        self.txn.intent = 'AUTHORIZE'
        self.txn.authorization_id = 'AUTH1'
        self.txn.capture_id = None
        self.txn.save()
        responses.add(responses.POST, f'{API}/v2/payments/authorizations/AUTH1/capture',
                      json={'id': 'CAP2', 'status': 'COMPLETED'})
        facade.capture_authorization(self.txn, amount=D('14.95'))
        assert last_json(responses, '/capture') == {
            'final_capture': True, 'amount': {'currency_code': 'EUR', 'value': '14.95'}, 'invoice_id': '100001'}
        txn = Transaction.objects.get()
        assert txn.capture_id == 'CAP2'
        assert txn.capture_status == 'COMPLETED'

    @responses.activate
    def test_void_authorization(self):
        mock_token(responses)
        self.txn.authorization_id = 'AUTH1'
        self.txn.save()
        responses.add(responses.POST, f'{API}/v2/payments/authorizations/AUTH1/void', status=204)
        facade.void_authorization(self.txn)
        assert Transaction.objects.get().status == 'VOIDED'

    @responses.activate
    def test_add_tracking(self):
        mock_token(responses)
        responses.add(responses.POST, f'{API}/v2/checkout/orders/{ORDER_ID}/track', json={'id': ORDER_ID})
        facade.add_tracking(self.txn, '00340434161094042557', 'DHL_API')
        assert last_json(responses, '/track') == {
            'capture_id': CAPTURE_ID, 'tracking_number': '00340434161094042557', 'carrier': 'DHL_API',
            'notify_payer': False}
        txn = Transaction.objects.get()
        assert txn.tracking_number == '00340434161094042557'
        assert txn.carrier == 'DHL_API'


@override_settings(**PAYPAL_SETTINGS, PAYPAL_SDK_LOCALE='de_DE')
class ButtonsTagTests(TestCase):

    def test_renders_sdk_with_settings(self):
        request = RequestFactory().get('/')
        request.basket = type('Basket', (), {'currency': 'EUR'})()
        html = Template('{% load paypal_tags %}{% paypal_buttons "#agb" %}').render(
            Context({'request': request, 'csrf_token': 'tok'}))
        assert 'client-id=client-id' in html
        assert 'currency=EUR' in html
        assert 'intent=capture' in html
        assert 'locale=de_DE' in html
        assert 'disable-funding=card%2Csepa%2Cvenmo' in html
        assert 'data-terms-checkbox="#agb"' in html
        assert reverse('express-checkout-capture-order') in html

    @override_settings(PAYPAL_CLIENT_ID='')
    def test_renders_nothing_without_credentials(self):
        html = Template('{% load paypal_tags %}{% paypal_buttons %}').render(Context({}))
        assert html.strip() == ''
