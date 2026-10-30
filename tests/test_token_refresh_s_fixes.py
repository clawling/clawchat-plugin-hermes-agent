"""Refresh envelope typing, JWT dates and per-generation proactive jitter."""
import base64
import io
import json
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from clawchat_gateway import api_client as api, config, connection, token_refresh as refresh


class Response(io.BytesIO):
    status = 200


def jwt(**claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return f'placeholder.{payload}.placeholder'


def http(monkeypatch, envelope, status=200):
    response = Response(json.dumps(envelope).encode())
    response.status = status
    monkeypatch.setattr(api, 'urlopen', lambda *_a, **_kw: response)
    return api.ClawChatApiClient(base_url='https://example.com', device_id='test-device')


@pytest.mark.parametrize('code,expected', [(10003,10003), (10003.0,10003), (400.0,400),
    (0.0,0), (1.0,1), (True,None), (False,None), ('10003',None), (10003.5,None),
    (float('nan'),None), (float('inf'),None), (None,None)])
def test_code_normalization_and_permanent_classifier(code, expected):
    assert api.normalize_refresh_code(code) == expected
    error = api.ClawChatApiError('auth', 'placeholder', code=code, status=200)
    assert api.is_permanent_refresh_error(error) == (expected in (10003,400))
    assert api._refresh_outcome_unknown(error) == (expected != 1)


@pytest.mark.parametrize('status', [200, 401, 503])
async def test_integral_float_revocation_stops_retry(monkeypatch, status):
    client = http(monkeypatch, {'code':10003.0, 'msg':'invalid placeholder token'}, status)
    async def exchange(*, refresh_token, device_id):
        return client._auth_refresh_sync(refresh_token, device_id)
    monkeypatch.setattr(client, 'auth_refresh', exchange)
    sleeper = AsyncMock()
    with pytest.raises(api.ClawChatApiError) as error:
        await api.auth_refresh_with_retry(client, refresh_token='placeholder-refresh',
                                         device_id='test-device', sleep=sleeper, max_transient_retries=0)
    assert error.value.code == 10003 and api.is_permanent_refresh_error(error.value)
    sleeper.assert_not_called()


@pytest.mark.parametrize('code', [True, False, '10003', float('inf'), float('nan'), 10003.5])
def test_unusable_json_codes_are_transient(monkeypatch, code):
    client = http(monkeypatch, {'code':code})
    with pytest.raises(api.ClawChatApiError) as error:
        client._auth_refresh_sync('placeholder-refresh', 'test-device')
    assert error.value.code is None and not api.is_permanent_refresh_error(error.value)


@pytest.mark.parametrize('field', ['access_token', 'refresh_token'])
@pytest.mark.parametrize('bad', [123, {'value':'placeholder'}, None, '   ', True, [], ''])
def test_rotated_tokens_require_nonblank_strings(monkeypatch, field, bad):
    data = {'access_token':'placeholder-access', 'refresh_token':'placeholder-refresh'}
    data[field] = bad
    client = http(monkeypatch, {'code':0.0, 'data':data})
    with pytest.raises(api.ClawChatApiError, match='invalid envelope: missing rotated tokens') as error:
        client._auth_refresh_sync('placeholder-old', 'test-device')
    assert error.value.kind == 'transport' and not api.is_permanent_refresh_error(error.value)


def test_valid_rotated_strings_are_trimmed(monkeypatch):
    client = http(monkeypatch, {'code':0.0,'data':{'access_token':' placeholder-access ',
                                                  'refresh_token':' placeholder-refresh '}})
    assert client._auth_refresh_sync('placeholder-old', 'test-device') == api.RefreshResult('placeholder-access','placeholder-refresh')


@pytest.mark.parametrize('claim', ['exp','iat'])
@pytest.mark.parametrize('value,expected', [(123,123),(123.75,123.75),('123',None),(True,None),
    (False,None),(10**400,None),(float('inf'),None),(float('-inf'),None),(float('nan'),None),(None,None),({},None)])
def test_jwt_claims_match_finite_number_typing(claim, value, expected):
    assert getattr(config, '_jwt_' + claim)(jwt(**{claim:value})) == expected


@pytest.mark.parametrize('iat', [None, 10000, 10001])
def test_invalid_lifetime_uses_fallback_and_expiry_fallback(iat):
    assert refresh.proactive_margin_seconds(10000, iat) == min(
        refresh.PROACTIVE_MAX_MARGIN_SECONDS, refresh.DEFAULT_ACCESS_TOKEN_TTL_SECONDS * 0.25)
    assert refresh.access_token_exp(jwt(exp=True), 1000000) == 1000 + refresh.DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    assert refresh.access_token_exp(jwt(exp='bad'), None) is None
    assert refresh.proactive_margin_seconds(10000, 9000) == 1800
    assert refresh.is_token_near_expiry(jwt(exp=10000,iat=0), None, now=7500)


@pytest.mark.parametrize('offset', [-300.0, 300.0])
async def test_connection_jitter_drawn_once_per_access_token(monkeypatch, offset):
    monkeypatch.setattr(connection, 'get_clawchat_store', lambda: None)
    draw = Mock(side_effect=[offset,-offset])
    cfg = config.ClawChatConfig(websocket_url='wss://example.com/ws', token=jwt(exp=10000,iat=0), refresh_token='placeholder-refresh')
    conn = connection.ClawChatConnection(cfg, on_message=AsyncMock(), proactive_jitter=draw)
    conn._refresh_manager = object()
    attempt = AsyncMock()
    monkeypatch.setattr(conn, '_attempt_refresh', attempt)
    # No jitter threshold would be 7500; either sign must shift the runtime check.
    clock = [7500 + offset - 0.5]
    monkeypatch.setattr(connection.time, 'time', lambda: clock[0])
    await conn._maybe_proactive_refresh()
    await conn._maybe_proactive_refresh()
    attempt.assert_not_called()
    assert draw.call_count == 1
    clock[0] += 0.5
    await conn._maybe_proactive_refresh()
    attempt.assert_awaited_once_with(close_ws_on_success=True)
    conn._cfg = replace(conn._cfg, refresh_token='replacement-refresh')
    await conn._maybe_proactive_refresh()
    assert draw.call_count == 1
    conn._cfg = replace(conn._cfg, token=jwt(exp=20000,iat=10000))
    await conn._maybe_proactive_refresh()
    assert draw.call_count == 2


@pytest.mark.parametrize('value', [True,False,float('inf'),float('-inf'),float('nan'),'123',None,-1,10**400])
def test_refusal_ignores_invalid_delay(value):
    assert connection.refusal_retry_after_seconds(json.dumps({'retry_after_ms':value})) == connection.TAKEOVER_BACKOFF_FLOOR_SECONDS


def test_refusal_uses_valid_numeric_delay_with_cap():
    assert connection.refusal_retry_after_seconds('{"retry_after_ms":1234.5}') == 1.2345
    assert connection.refusal_retry_after_seconds('{"retry_after_ms":999999999}') == connection.TAKEOVER_BACKOFF_MAX_SECONDS
