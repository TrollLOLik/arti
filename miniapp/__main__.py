"""Run an isolated localhost demo: python -m miniapp --demo."""
import argparse
from aiohttp import web
from .server import Settings, create_app


def main():
    parser = argparse.ArgumentParser(description='Арти Mini App — локальный просмотр. Боевой API запускается вместе с main.py.')
    parser.add_argument('--demo', action='store_true', help='Разрешить изолированные демонстрационные данные')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not args.demo:
        parser.error('Для предпросмотра укажи --demo. Для Telegram настрой ARTI_MINIAPP_ENABLED и запусти main.py.')
    if args.host not in ('127.0.0.1', 'localhost', '::1'):
        parser.error('Деморежим доступен только на loopback адресе.')
    web.run_app(create_app(Settings(demo=True)), host=args.host, port=args.port, access_log=None)


if __name__ == '__main__':
    main()
