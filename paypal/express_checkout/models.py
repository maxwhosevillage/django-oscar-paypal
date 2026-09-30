from django.db import models


class ExpressCheckoutTransaction(models.Model):
    """
    Dedicated to store transactions for Express Checkout SDK that are
    related to the same Oscar's order.
    """

    # ID of PayPal's order instance
    order_id = models.CharField(max_length=255)
    authorization_id = models.CharField(max_length=255, null=True, blank=True)
    capture_id = models.CharField(max_length=255, null=True, blank=True)
    refund_id = models.CharField(max_length=255, null=True, blank=True)
    payer_id = models.CharField(max_length=255, null=True, blank=True)

    email = models.EmailField(null=True, blank=True)

    # Oscar order number (sent to PayPal as invoice_id) and the basket paid for
    order_number = models.CharField(max_length=128, blank=True, db_index=True)
    basket_id = models.PositiveIntegerField(null=True, blank=True)

    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=8, null=True, blank=True)

    CREATED, SAVED, APPROVED, VOIDED, COMPLETED = 'CREATED', 'SAVED', 'APPROVED', 'VOIDED', 'COMPLETED'
    status = models.CharField(max_length=255)
    # Status of the capture itself: COMPLETED, PENDING (money not yet available), DECLINED, ...
    capture_status = models.CharField(max_length=32, blank=True)

    AUTHORIZE, CAPTURE = 'AUTHORIZE', 'CAPTURE'
    intent = models.CharField(max_length=9)

    address_full_name = models.CharField(max_length=255, blank=True)
    address = models.TextField(blank=True)

    tracking_number = models.CharField(max_length=128, blank=True)
    carrier = models.CharField(max_length=64, blank=True)

    date_created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-date_created',)
        app_label = 'paypal'

    def __str__(self):
        return f'PayPal {self.order_id} ({self.intent}, {self.status})'

    @property
    def is_authorization(self):
        return self.intent == self.AUTHORIZE

    @property
    def is_completed(self):
        return self.status == self.COMPLETED

    @property
    def is_capture_pending(self):
        return self.capture_status == 'PENDING'
