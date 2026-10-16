import hashlib
import hmac
import json
import unittest
from urllib.parse import urlencode

from miniapp.auth import AuthError, validate_init_data
from miniapp.settings import public_url

TOKEN = '12345:test-token-not-a-secret'
NOW = 1800000000


def signed(user=None, date=NOW, **extra):
    fields = dict(auth_date=str(date), user=json.dumps(user or dict(id=42, first_name='Тест'), ensure_ascii=False), **extra)
    secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, '\n'.join(f'{k}={v}' for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class AuthTests(unittest.TestCase):
    def test_signed_unicode_identity(self):
        self.assertEqual(dict(id=42, first_name='Тест'), validate_init_data(signed(), TOKEN, now=NOW))

    def test_tampering_other_bot_duplicates_and_expiry_fail(self):
        cases = [signed().replace('42', '43'), signed()+'&auth_date='+str(NOW), signed(date=NOW-3601), signed(date=NOW+31), '', 'user=%7B%7D', signed()+ '&user=%7B%7D']
        for raw in cases:
            with self.subTest(raw=raw[:35]), self.assertRaises(AuthError):
                validate_init_data(raw, TOKEN, now=NOW)
        with self.assertRaises(AuthError):
            validate_init_data(signed(), 'another-bot', now=NOW)

    def test_signed_invalid_users_fail(self):
        for user in (dict(id=True), dict(id=-42), dict(id='42'), dict(id=42, is_bot=True), dict(id=2**64)):
            with self.subTest(user=user), self.assertRaises(AuthError):
                validate_init_data(signed(user), TOKEN, now=NOW)

    def test_signature_field_is_part_of_bot_hmac(self):
        self.assertEqual(42, validate_init_data(signed(signature='abc'), TOKEN, now=NOW)['id'])

    def test_public_url_requires_clean_https_origin(self):
        self.assertEqual('https://arti.example.com', public_url('https://arti.example.com/'))
        for url in ('http://example.com', 'https://u:p@example.com', 'https://example.com/path', 'https://example.com?demo=1', 'https://example.com#fragment', ''):
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_url(url)
