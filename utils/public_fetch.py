"""Public-only, pinned DNS connections and bounded redirect/byte/time handling."""
import asyncio,ipaddress,socket
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlsplit,urljoin
import aiohttp
from aiohttp.abc import AbstractResolver
from materials.types import MaterialError
from utils.url_safety import _ip_is_public


def validate_url(url):
    try:
        p=urlsplit(url)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.port not in (None,80,443) or len(url)>4096: raise ValueError()
        try:
            ipaddress.ip_address(p.hostname)
            if not _ip_is_public(p.hostname): raise ValueError()
        except ValueError as exc:
            # An IP literal cannot fall through to hostname resolution.
            if ':' in p.hostname or all(c in '0123456789.' for c in p.hostname): raise exc
        return p
    except (TypeError,ValueError) as exc: raise MaterialError('public_url_denied') from exc


class PublicResolver(AbstractResolver):
    async def resolve(self,host,port=0,family=socket.AF_UNSPEC):
        infos=await asyncio.get_running_loop().getaddrinfo(host,port,family=family,type=socket.SOCK_STREAM)
        if not infos or any(not _ip_is_public(i[4][0]) for i in infos): raise MaterialError('public_dns_denied')
        return [dict(hostname=host,host=i[4][0],port=port,family=i[0],proto=i[2],flags=socket.AI_NUMERICHOST) for i in infos]
    async def close(self): pass


class PublicConnector(aiohttp.TCPConnector):
    async def _wrap_create_connection(self,*args,**kwargs):
        # Addresses are the exact immutable resolver result passed to socket connect.
        infos=kwargs.get('addr_infos',())
        if not infos or any(not _ip_is_public(i[4][0]) for i in infos): raise MaterialError('public_connection_denied')
        transport,protocol=await super()._wrap_create_connection(*args,**kwargs)
        peer=transport.get_extra_info('peername')
        allowed={str(ipaddress.ip_address(i[4][0])) for i in infos}
        if not peer or not _ip_is_public(peer[0]) or str(ipaddress.ip_address(peer[0])) not in allowed:
            transport.close(); raise MaterialError('public_peer_denied')
        return transport,protocol


@dataclass(frozen=True)
class FetchedResource:
    data: bytes
    url: str
    mime: str
    sha256: str
    redirects: tuple[str,...]


async def fetch_public(url,*,max_bytes=10*1024**2,timeout_seconds=30,allowed_mimes=None,validate=None,session_factory=None):
    if not 1<=max_bytes<=20*1024**2 or not 1<=timeout_seconds<=90: raise MaterialError('fetch_budget')
    if validate: await validate()
    connector=PublicConnector(resolver=PublicResolver(),use_dns_cache=False,force_close=True,limit=2)
    factory=session_factory or aiohttp.ClientSession
    redirects=[]
    try:
        async with asyncio.timeout(timeout_seconds):
            async with factory(connector=connector,trust_env=False,auto_decompress=False,cookie_jar=aiohttp.DummyCookieJar(),timeout=aiohttp.ClientTimeout(total=timeout_seconds,sock_connect=10,sock_read=10)) as session:
                for hop in range(4):
                    validate_url(url)
                    async with session.get(url,allow_redirects=False,headers={'Accept-Encoding':'identity'}) as response:
                        if response.status in (301,302,303,307,308):
                            location=response.headers.get('Location')
                            if not location or hop==3: raise MaterialError('fetch_redirect_budget')
                            redirects.append(url); url=urljoin(url,location); continue
                        if response.status!=200: raise MaterialError('fetch_status')
                        mime=response.headers.get('Content-Type','application/octet-stream').split(';')[0].lower()
                        if allowed_mimes and mime not in allowed_mimes: raise MaterialError('fetch_mime_denied')
                        if response.headers.get('Content-Encoding','identity').lower()!='identity': raise MaterialError('fetch_encoding_denied')
                        length=response.headers.get('Content-Length')
                        if length and (not length.isdigit() or int(length)>max_bytes): raise MaterialError('fetch_byte_budget')
                        content=bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            content.extend(chunk)
                            if len(content)>max_bytes: raise MaterialError('fetch_byte_budget')
                        if not content: raise MaterialError('fetch_empty')
                        if validate: await validate()
                        data=bytes(content)
                        return FetchedResource(data,url,mime,sha256(data).hexdigest(),tuple(redirects))
    finally:
        await connector.close()
    raise MaterialError('fetch_unavailable')
