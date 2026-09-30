from django.apps import AppConfig


class PaypalConfig(AppConfig):
    name = 'paypal'
    verbose_name = 'PayPal'
    # Keep the existing integer primary keys regardless of DEFAULT_AUTO_FIELD
    default_auto_field = 'django.db.models.AutoField'
