from urllib.parse import parse_qsl

from django.db import models
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _


class ResponseModel(models.Model):

    # Debug information
    raw_request = models.TextField(max_length=512)
    raw_response = models.TextField(max_length=512)

    response_time = models.FloatField(help_text=_("Response time in milliseconds"))

    date_created = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        ordering = ('-date_created',)
        app_label = 'paypal'

    def request(self):
        request_params = self.context
        return self._as_dl(request_params)

    def response(self):
        return self._as_dl(self.context)

    def _as_table(self, params):
        rows = format_html_join(
            '', '<tbody><tr><th>{}</th><td>{}</td></tr></tbody>', ((k, v[0]) for k, v in sorted(params.items())))
        return format_html('<table>{}</table>', rows)

    def _as_dl(self, params):
        rows = format_html_join('', '<dt>{}</dt><dd>{}</dd>', ((k, v[0]) for k, v in sorted(params.items())))
        return format_html('<dl>{}</dl>', rows)

    @property
    def context(self):
        ctx = {}
        for key, val in parse_qsl(self.raw_response):
            ctx[key] = [val]
        return ctx

    def value(self, key, default=None):
        ctx = self.context
        return ctx[key][0] if key in ctx else default
