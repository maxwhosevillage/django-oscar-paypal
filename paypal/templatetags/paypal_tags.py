from urllib.parse import urlencode

from django import template
from django.conf import settings

register = template.Library()

SDK_URL = 'https://www.paypal.com/sdk/js'


def sdk_url(currency):
    params = {
        'client-id': settings.PAYPAL_CLIENT_ID,
        'currency': currency,
        'intent': getattr(settings, 'PAYPAL_ORDER_INTENT', 'CAPTURE').lower(),
        'commit': 'true',
        'components': 'buttons',
    }
    for setting, param, default in (
            ('PAYPAL_SDK_LOCALE', 'locale', None),
            ('PAYPAL_ENABLE_FUNDING', 'enable-funding', 'paylater'),
            # Cards etc. are offered by the shop's own payment provider
            ('PAYPAL_DISABLE_FUNDING', 'disable-funding', 'card,sepa,venmo'),
    ):
        value = getattr(settings, setting, default)
        if value:
            params[param] = value
    return f'{SDK_URL}?{urlencode(params)}'


@register.inclusion_tag('paypal/express_checkout/buttons.html', takes_context=True)
def paypal_buttons(context, terms_checkbox=''):
    """
    Render the PayPal JS SDK buttons for the current checkout.

    ``terms_checkbox`` is an optional CSS selector of a checkbox (e.g. terms
    and conditions) that must be checked before the PayPal popup opens.
    """
    if not getattr(settings, 'PAYPAL_CLIENT_ID', ''):
        return {'sdk_url': None}
    request = context['request']
    currency = request.basket.currency or getattr(settings, 'PAYPAL_CURRENCY', 'EUR')
    return {
        'request': request,
        'csrf_token': context.get('csrf_token'),
        'sdk_url': sdk_url(currency),
        'terms_checkbox': terms_checkbox,
        'button_style': getattr(settings, 'PAYPAL_BUTTON_STYLE', {
            'layout': 'vertical', 'shape': 'rect', 'label': 'paypal', 'height': 45}),
    }
