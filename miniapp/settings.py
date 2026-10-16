"""Small standalone URL validation, safe to import from the native bot menu."""
from urllib.parse import urlsplit


def public_url(value):
    parts = urlsplit(value)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('', '/'):
        raise ValueError('ARTI_MINIAPP_URL must be an HTTPS origin, e.g. https://arti.example.com')
    return value.rstrip('/')
