"""Documentation works directly and through the deployer's exact proxy routes."""

from contextlib import contextmanager
import re
from urllib.parse import urljoin

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from app.main import create_app


class Gateway:
    def __init__(self, app, prefix):
        self.app, self.prefix = app, prefix

    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        if scope['path'] in {self.prefix + p for p in ['/docs', '/redoc', '/openapi.json']}:
            scope['path'] = scope['path'][len(self.prefix):]
            scope['raw_path'] = scope['path'].encode()
        elif scope['path'] != '/api/v1/worker/status':
            await Response(status_code=404)(scope, receive, send)
            return
        await self.app(scope, receive, send)


@contextmanager
def documentation_client(app):
    # No lifespan: documentation requires no database, RPC or storage startup.
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()


@pytest.mark.parametrize('prefix', ['', '/api', '/tools/minter'])
@pytest.mark.parametrize('page', ['docs', 'redoc'])
def test_schema_loads_at_each_mount_without_prefixing_api_twice(prefix, page):
    with documentation_client(Gateway(create_app(), prefix)) as client:
        public_url = 'http://testserver' + prefix + '/' + page
        response = client.get(public_url)
        assert response.status_code == 200
        pattern = r"url:\s*['\"]([^'\"]+)" if page == 'docs' else r'spec-url="([^"]+)"'
        schema_url = urljoin(public_url, re.search(pattern, response.text).group(1))
        assert schema_url == 'http://testserver' + prefix + '/openapi.json'
        schema_response = client.get(schema_url)
        assert schema_response.status_code == 200
        schema = schema_response.json()
        assert '/api/v1/arks' in schema['paths']
        assert not any(path.startswith('/api/api/') for path in schema['paths'])
        assert '/docs' not in schema['paths']
        assert '/redoc' not in schema['paths']
        assert not schema.get('servers')
