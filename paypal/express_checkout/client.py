"""
Minimal client for PayPal's REST API (OAuth2 + Orders v2 + Payments v2).

Replaces the deprecated ``paypal-checkout-serversdk``. Responses are returned
as plain dicts exactly as PayPal sends them.
"""
import hashlib
import logging

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger('paypal.express_checkout')

SANDBOX_BASE_URL = 'https://api-m.sandbox.paypal.com'
LIVE_BASE_URL = 'https://api-m.paypal.com'


class PayPalError(Exception):
    """
    Error response from PayPal. ``name`` is PayPal's error name (e.g.
    ``UNPROCESSABLE_ENTITY``), ``issue`` the first detail issue (e.g.
    ``INSTRUMENT_DECLINED``), ``debug_id`` is what PayPal support asks for.
    """

    def __init__(self, message, status_code=None, name=None, details=None, debug_id=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.name = name
        self.details = details or []
        self.debug_id = debug_id

    @property
    def issue(self):
        for detail in self.details:
            if detail.get('issue'):
                return detail['issue']
        return None

    def __str__(self):
        parts = [self.message]
        if self.issue:
            parts.append(f'issue={self.issue}')
        fields = [detail['field'] for detail in self.details if detail.get('field')]
        if fields:
            parts.append(f'fields={",".join(fields)}')
        if self.debug_id:
            parts.append(f'debug_id={self.debug_id}')
        return ' '.join(parts)

    @classmethod
    def from_response(cls, response):
        try:
            data = response.json()
        except ValueError:
            data = {}
        return cls(
            message=data.get('message') or data.get('error_description') or response.reason or 'PayPal error',
            status_code=response.status_code,
            name=data.get('name') or data.get('error'),
            details=data.get('details'),
            debug_id=data.get('debug_id') or response.headers.get('PayPal-Debug-Id'),
        )


class PayPalClient:
    timeout = 30

    def __init__(self, client_id, client_secret, sandbox=True, session=None):
        if not client_id or not client_secret:
            raise ImproperlyConfigured('PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set')
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = SANDBOX_BASE_URL if sandbox else LIVE_BASE_URL
        self.session = session or requests.Session()

    @classmethod
    def from_settings(cls):
        return cls(
            client_id=getattr(settings, 'PAYPAL_CLIENT_ID', ''),
            client_secret=getattr(settings, 'PAYPAL_CLIENT_SECRET', ''),
            sandbox=getattr(settings, 'PAYPAL_SANDBOX_MODE', True),
        )

    @property
    def _token_cache_key(self):
        digest = hashlib.sha256(f'{self.base_url}:{self.client_id}'.encode()).hexdigest()[:16]
        return f'paypal-access-token-{digest}'

    def get_access_token(self, force_refresh=False):
        if not force_refresh:
            token = cache.get(self._token_cache_key)
            if token:
                return token

        response = self.session.post(
            f'{self.base_url}/v1/oauth2/token',
            data={'grant_type': 'client_credentials'},
            auth=(self.client_id, self.client_secret),
            headers={'Accept': 'application/json'},
            timeout=self.timeout,
        )
        if not response.ok:
            raise PayPalError.from_response(response)
        data = response.json()
        # Refresh a minute before PayPal expires the token
        cache.set(self._token_cache_key, data['access_token'], max(int(data.get('expires_in', 0)) - 60, 0))
        return data['access_token']

    def request(self, method, path, json=None, request_id=None, prefer='return=representation'):
        """
        Send an authenticated request. ``request_id`` is sent as
        ``PayPal-Request-Id`` so PayPal treats retries of the same call as
        idempotent (e.g. a capture is never executed twice).
        """
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if prefer:
            headers['Prefer'] = prefer
        if request_id:
            headers['PayPal-Request-Id'] = request_id

        for attempt in (1, 2):
            headers['Authorization'] = f'Bearer {self.get_access_token(force_refresh=attempt > 1)}'
            response = self.session.request(
                method, f'{self.base_url}{path}', json=json, headers=headers, timeout=self.timeout)
            # An expired/revoked cached token: fetch a fresh one and retry once
            if response.status_code != 401:
                break

        if not response.ok:
            error = PayPalError.from_response(response)
            logger.warning('PayPal %s %s failed: %s', method, path, error)
            raise error
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def get(self, path, **kwargs):
        return self.request('GET', path, **kwargs)

    def post(self, path, json=None, **kwargs):
        return self.request('POST', path, json=json if json is not None else {}, **kwargs)
