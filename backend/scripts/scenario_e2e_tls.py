"""Task-local verified HTTPS bridge for synthetic plugin acceptance."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import httpx
import certifi
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route
import uvicorn


def serve(upstream: str, directory: Path, port: int) -> None:
    parsed = urlsplit(upstream)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {'', '/'}):
        raise ValueError('Acceptance upstream must be an explicit loopback HTTP origin')
    directory.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Synthetic scenario acceptance')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(private_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=8))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address('127.0.0.1'))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(private_key, hashes.SHA256()))
    certificate_path = directory / 'certificate.pem'
    private_key_path = directory / 'private-key.pem'
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (directory / 'ca-bundle.pem').write_bytes(Path(certifi.where()).read_bytes()
        + b'\n' + certificate_path.read_bytes())
    private_key_path.write_bytes(private_key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    async def proxy(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 256 * 1024:
                return Response(status_code=413)
        client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5),
            follow_redirects=False, trust_env=False)
        excluded = {'host', 'connection', 'content-length', 'accept-encoding'}
        headers = {key: value for key, value in request.headers.items() if key.lower() not in excluded}
        target = upstream.rstrip('/') + request.url.path
        if request.url.query:
            target += '?' + request.url.query
        try:
            response = await client.send(client.build_request(request.method, target,
                headers=headers, content=bytes(body)), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            return Response(status_code=502)

        async def content():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()
        excluded_response = {'connection', 'transfer-encoding', 'content-length', 'content-encoding'}
        returned = {key: value for key, value in response.headers.items()
            if key.lower() not in excluded_response}
        return StreamingResponse(content(), status_code=response.status_code, headers=returned)

    app = Starlette(routes=[Route('/{path:path}', proxy,
        methods=['GET', 'POST', 'PATCH', 'DELETE', 'OPTIONS'])])
    try:
        uvicorn.run(app, host='127.0.0.1', port=port, ssl_certfile=str(certificate_path),
            ssl_keyfile=str(private_key_path), log_level='error', access_log=False)
    finally:
        private_key_path.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', required=True)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--port', required=True, type=int)
    arguments = parser.parse_args()
    serve(arguments.upstream, arguments.directory, arguments.port)
