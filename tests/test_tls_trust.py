"""The default HTTPX clients must use certifi's patched TLS trust bundle."""
import asyncio
import ssl

import certifi
import httpx


COMPROMISED_ROOT = "emSign ECC Root CA - G3"
UNAFFECTED_ROOT = "emSign Root CA - G1"


def assert_patched_context(context):
    names = {
        value
        for certificate in context.get_ca_certs()
        for item in certificate.get("subject", ())
        for key, value in item
        if key == "commonName"
    }
    assert COMPROMISED_ROOT not in names
    assert UNAFFECTED_ROOT in names
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_certifi_bundle_excludes_compromised_root_and_keeps_unaffected_root():
    assert_patched_context(ssl.create_default_context(cafile=certifi.where()))


def test_httpx_sync_client_uses_patched_default_trust():
    with httpx.Client() as client:
        assert_patched_context(client._transport._pool._ssl_context)


def test_httpx_async_client_uses_patched_default_trust():
    async def check_client():
        async with httpx.AsyncClient() as client:
            assert_patched_context(client._transport._pool._ssl_context)

    asyncio.run(check_client())
