"""Protection pages work with the application's script-src 'self' policy."""
import asyncio
import re

import httpx
import pytest

from sjfx_protection.tests.test_access import client, env


@pytest.mark.parametrize('page', ['packages', 'admin'])
def test_pages_load_authenticated_same_origin_scripts(env, page):
    async def run():
        async with client(env[0], 'admin') as c:
            response = await c.get('/protection/' + page)
            assert response.status_code == 200
            assert f'<script src="/protection/{page}.js"></script>' in response.text
            assert not re.search(r'<script(?:\s[^>]*)?>(?!\s*</script>)', response.text)
            script = await c.get('/protection/' + page + '.js')
            assert script.status_code == 200
            assert 'application/javascript' in script.headers['content-type']
            assert 'no-store' in script.headers['cache-control']
            assert "fetch('/protection/api/" in script.text
            assert "credentials:'same-origin'" in script.text
            assert (await c.post('/protection/' + page + '.js')).status_code == 405
    asyncio.run(run())


def test_script_assets_preserve_session_and_admin_boundaries(env):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env[0]), base_url='http://test') as c:
            for name in ('packages', 'admin'):
                assert (await c.get('/protection/' + name + '.js')).status_code == 401
        async with client(env[0], 'user') as c:
            assert (await c.get('/protection/packages.js')).status_code == 200
            assert (await c.get('/protection/admin.js')).status_code == 403
            assert (await c.get('/protection/api/events')).status_code == 403
    asyncio.run(run())
