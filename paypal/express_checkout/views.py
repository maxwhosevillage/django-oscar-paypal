"""
Server side of the PayPal JS SDK buttons, shown on the checkout preview page
once shipping address and method are known:

1. ``createOrder`` -> ``CreateOrderView`` registers the basket with PayPal
   (items, shipping address, order number) and returns the PayPal order id.
2. The buyer approves in the PayPal popup.
3. ``onApprove`` -> ``CaptureOrderView`` places the Oscar order. The payment is
   captured inside ``handle_payment``, so an order only exists once PayPal has
   taken the money.

Both views answer with JSON: ``{"id": ...}``, ``{"redirect": url}`` or
``{"error": message, "restart": bool}``. ``restart`` tells the JS to call
``actions.restart()`` so the buyer can choose another funding source.
"""
import logging

from django.http import HttpResponseRedirect, JsonResponse
from django.utils.translation import gettext_lazy as _
from oscar.apps.payment.exceptions import UnableToTakePayment
from oscar.core.loading import get_class, get_model

from paypal.express_checkout import facade
from paypal.express_checkout.client import PayPalError
from paypal.express_checkout.models import ExpressCheckoutTransaction as Transaction

PaymentDetailsView = get_class('checkout.views', 'PaymentDetailsView')

Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')

logger = logging.getLogger('paypal.express_checkout')

SESSION_KEY = 'order_id'
SESSION_NAMESPACE = 'paypal'
# PayPal issues after which the buyer can pick another funding source in the popup
RESTARTABLE_ISSUES = ('INSTRUMENT_DECLINED', 'PAYER_ACTION_REQUIRED')


class JsonCheckoutMixin:
    """
    Run as the preview step of the shop's checkout, but answer every redirect
    (failed pre-conditions, order placed) as JSON for the JS SDK.
    """
    http_method_names = ['post']
    preview = True

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        if isinstance(response, HttpResponseRedirect):
            return JsonResponse({'redirect': response.url})
        return response

    def error(self, message, restart=False, status=400):
        return JsonResponse({'error': str(message), 'restart': restart}, status=status)


class CreateOrderView(JsonCheckoutMixin, PaymentDetailsView):

    def post(self, request, *args, **kwargs):
        submission = self.build_submission()
        basket = submission['basket']
        shipping_required = basket.is_shipping_required()
        user = submission['user']
        email = user.email if user.is_authenticated else submission['order_kwargs'].get('guest_email')

        try:
            txn = facade.create_order(
                basket,
                order_total=submission['order_total'].incl_tax,
                shipping_charge=submission['shipping_charge'].incl_tax if shipping_required else None,
                surcharges=submission['surcharges'],
                shipping_address=submission['shipping_address'] if shipping_required else None,
                billing_address=submission['payment_kwargs'].get('billing_address'),
                email=email,
                order_number=self.generate_order_number(basket),
                absolute_uri=request.build_absolute_uri,
            )
        except PayPalError as e:
            logger.warning('Basket #%s: unable to create PayPal order: %s', basket.id, e)
            return self.error(_('A problem occurred communicating with PayPal - please try again later'), status=502)

        self.checkout_session._set(SESSION_NAMESPACE, SESSION_KEY, txn.order_id)
        return JsonResponse({'id': txn.order_id})


class CaptureOrderView(JsonCheckoutMixin, PaymentDetailsView):
    txn = None
    paypal_error = None

    def post(self, request, *args, **kwargs):
        order_id = request.POST.get('order_id')
        # Only the order created in this checkout session can be paid here
        if not order_id or order_id != self.checkout_session._get(SESSION_NAMESPACE, SESSION_KEY):
            logger.warning('PayPal order %s does not belong to this checkout session', order_id)
            return self.error(_('Unable to determine PayPal transaction details'))

        basket = request.basket
        try:
            self.txn = Transaction.objects.get(order_id=order_id, basket_id=basket.id)
        except Transaction.DoesNotExist:
            return self.error(_('No basket was found that corresponds to your PayPal transaction'))

        submission = self.build_submission(basket=basket)
        # The basket may have changed in another tab after the PayPal order
        # was created - never capture an amount that differs from the order.
        total = submission['order_total']
        if facade.to_decimal(total.incl_tax) != self.txn.amount or facade.get_currency(basket) != self.txn.currency:
            logger.warning(
                'Basket #%s: total %s differs from PayPal order %s over %s',
                basket.id, total.incl_tax, order_id, self.txn.amount)
            return self.error(_('Your basket has changed - please check your order and pay again'))

        response = self.submit(**submission)
        if isinstance(response, HttpResponseRedirect):
            return response

        # submit() rendered the payment/preview page with an error
        if self.paypal_error is not None:
            restart = self.paypal_error.issue in RESTARTABLE_ISSUES
            if restart:
                return self.error(_('Your payment was declined by PayPal - please choose another payment method'),
                                  restart=True)
        error = response.context_data.get('error') if hasattr(response, 'context_data') else None
        return self.error(error or _('A problem occurred during payment capturing - please try again later'))

    def handle_payment(self, order_number, total, **kwargs):
        txn = self.txn
        if txn.order_number and txn.order_number != order_number:
            logger.warning('PayPal order %s was created for order #%s, placing #%s',
                           txn.order_id, txn.order_number, order_number)
            txn.order_number = order_number
            txn.save(update_fields=['order_number'])

        try:
            facade.complete_payment(txn)
        except PayPalError as e:
            self.paypal_error = e
            logger.warning('Order #%s: PayPal refused payment for %s: %s', order_number, txn.order_id, e)
            raise UnableToTakePayment(_('A problem occurred during payment capturing - please try again later'))

        if txn.is_authorization:
            event_type = 'Authorised'
            debited = 0
            reference = txn.authorization_id
        elif txn.capture_status in ('COMPLETED', 'PENDING'):
            event_type = 'Settled' if txn.capture_status == 'COMPLETED' else 'Pending'
            debited = txn.amount
            reference = txn.capture_id
        else:
            raise UnableToTakePayment(_('Your payment was declined by PayPal - please choose another payment method'))

        source_type, __ = SourceType.objects.get_or_create(name='PayPal')
        self.add_payment_source(Source(
            source_type=source_type,
            currency=txn.currency,
            amount_allocated=txn.amount,
            amount_debited=debited,
            reference=txn.order_id,
        ))
        self.add_payment_event(event_type, txn.amount, reference=reference)

    def handle_order_placement(self, order_number, *args, **kwargs):
        try:
            return super().handle_order_placement(order_number, *args, **kwargs)
        except Exception:
            # The money is taken at this point - make sure this gets noticed
            logger.critical(
                'Order #%s: PayPal payment %s captured but order placement failed',
                order_number, self.txn.capture_id or self.txn.authorization_id, exc_info=True)
            raise
