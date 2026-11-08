import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from starlette.requests import Request
from dark_core_lib.request_signing import sign_request
from app.middleware import signed_auth, auth


def request(headers, body=b'{}', path=b'/api/v1/arks'):
    async def receive():
        return {'type':'http.request','body':body,'more_body':False}
    return Request({'type':'http','method':'POST','path':path.decode(),'raw_path':path,
        'query_string':b'', 'headers':[(k.lower().encode(),v.encode()) for k,v in headers.items()]},receive)


@pytest.fixture
def setup(monkeypatch):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    settings = SimpleNamespace(minter_signed_auth_enabled=True,
        minter_signed_auth_max_skew_seconds=300,minter_client_key_cache_idle_ttl_seconds=60)
    monkeypatch.setattr(signed_auth,'get_settings',lambda:settings)
    registry = Mock(get_authority_auth=Mock(return_value=(public,True)))
    monkeypatch.setattr(signed_auth,'get_corelib_client',lambda:registry)
    used=set()
    def claim(authority,nonce,expiry):
        if (authority,nonce) in used: raise signed_auth._error(409,'SIGNED_REQUEST_REPLAY')
        used.add((authority,nonce))
    monkeypatch.setattr(signed_auth,'_claim_nonce',claim)
    signed_auth._cache.clear()
    return key,registry


def headers(key):
    return sign_request(key,'POST','/api/v1/arks',b'{}','authority')


@pytest.mark.asyncio
async def test_valid_signature_and_replay(setup):
    key,registry=setup; h=headers(key)
    assert (await signed_auth.authenticate(request(h)))['authority_id']=='authority'
    previous=signed_auth._cache['authority'][2]
    with pytest.raises(HTTPException) as exc: await signed_auth.authenticate(request(h))
    assert exc.value.status_code==409
    assert signed_auth._cache['authority'][2]==previous
    assert registry.get_authority_auth.call_count==1


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['body','path','signature','timestamp','authority'])
async def test_tampering(setup,change):
    key,_=setup;h=headers(key);body=b'{}';path=b'/api/v1/arks'
    if change=='body':body=b'{"changed":true}'
    elif change=='path':path=b'/api/v1/arks/batch'
    elif change=='signature':h['X-DARK-Signature']='A'*88
    elif change=='timestamp':h['X-DARK-Timestamp']=str(int(time.time())-1000)
    elif change=='authority':h['X-DARK-Authority-Id']='other'
    with pytest.raises(HTTPException) as exc:await signed_auth.authenticate(request(h,body,path))
    assert exc.value.status_code==401
    assert not signed_auth._cache


@pytest.mark.asyncio
async def test_sliding_cache(setup,monkeypatch):
    key,registry=setup;clock=[1000.0]
    monkeypatch.setattr(signed_auth.time,'monotonic',lambda:clock[0])
    async def send():return await signed_auth.authenticate(request(headers(key)))
    await send();registry.get_authority_auth.return_value=(bytes([1])*32,True)
    for _ in range(4):clock[0]+=50;await send()
    assert registry.get_authority_auth.call_count==1
    clock[0]+=61
    with pytest.raises(HTTPException):await send()
    assert registry.get_authority_auth.call_count==2


@pytest.mark.asyncio
async def test_disabled_keeps_identity(monkeypatch):
    monkeypatch.setattr(auth,'get_settings',lambda:SimpleNamespace(minter_signed_auth_enabled=False))
    assert (await auth.require_authority_identity(request({'X-DARK-Authority-Id':'authority'}),None))['auth_mode']=='header'
    with pytest.raises(HTTPException):await auth.require_authority_identity(request({}),None)



@pytest.mark.asyncio
async def test_enabled_dependency_uses_signature(setup,monkeypatch):
    key,_=setup
    monkeypatch.setattr(auth,'get_settings',signed_auth.get_settings)
    assert (await auth.require_authority_identity(request(headers(key)),None))['auth_mode']=='signed'
    with pytest.raises(HTTPException) as exc:
        await auth.require_authority_identity(request({'X-DARK-Authority-Id':'authority'}),None)
    assert exc.value.status_code==401


@pytest.mark.asyncio
async def test_bad_signature_does_not_extend_cached_key(setup):
    key,_=setup
    await signed_auth.authenticate(request(headers(key)))
    previous=signed_auth._cache['authority'][2]
    h=headers(key);h['X-DARK-Signature']='A'*88
    with pytest.raises(HTTPException):await signed_auth.authenticate(request(h))
    assert signed_auth._cache['authority'][2]==previous


@pytest.mark.asyncio
async def test_inactive_registry_rejected(setup):
    key,registry=setup
    registry.get_authority_auth.return_value=(registry.get_authority_auth.return_value[0],False)
    with pytest.raises(HTTPException):await signed_auth.authenticate(request(headers(key)))
    assert not signed_auth._cache


@pytest.mark.parametrize('ark', ['ark:/12345/example', 'ark:12345/example'])
def test_retry_cannot_change_another_authority(monkeypatch, ark):
    from contextlib import contextmanager
    from app.api import worker
    record=SimpleNamespace(authority_id='other-authority',processing_status=3)
    db=Mock()
    @contextmanager
    def locked(ark):yield db
    monkeypatch.setattr(worker,'acquire_ark_lock',locked)
    monkeypatch.setattr(worker,'ARKRepository',lambda db:SimpleNamespace(get_by_ark=lambda ark:record))
    result=worker.retry_failed_arks(SimpleNamespace(arks=[ark],reason='retry'),{'authority_id':'caller'})
    assert result['items']==[{'ark':'ark:12345/example','status':'forbidden'}]
    db.commit.assert_not_called()
    assert record.processing_status==3
