"""Remote refresh diagnostics cannot carry credentials into exceptions or logs."""
import base64
import io
import json
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.error import HTTPError, URLError

import pytest

from clawchat_gateway import api_client as api
from clawchat_gateway.token_refresh import RefreshManager

REFRESH = 'placeholder-refresh-secret'
ACCESS = 'placeholder-access-secret'
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


# Built at runtime so no token-shaped literal sits in the tracked tree.
JWT = '.'.join(_b64(part) for part in (b'{"placeholder":true}', b'{"exp":12345}', b'placeholder'))
OPAQUE = 'obvious_placeholder_opaque_token_value_1234567890'
MESSAGE = f' rejected\n {REFRESH} {ACCESS} {JWT} {OPAQUE} '


class Response(io.BytesIO):
    status = 200


def client():
    return api.ClawChatApiClient(base_url='https://example.com', token=ACCESS, device_id='test-device')


@pytest.mark.parametrize('code,status', [(10003, 200), (10003, 401), (400, 200), (1, 200), (9, 200)])
async def test_server_message_is_redacted_at_exception_and_manager_log(monkeypatch, caplog, code, status):
    body = json.dumps({'code': code, 'msg': MESSAGE}).encode()
    def urlopen(request, **kwargs):
        if status != 200:
            raise HTTPError(request.full_url, status, 'placeholder', {}, Response(body))
        return Response(body)
    monkeypatch.setattr(api, 'urlopen', urlopen)
    http = client()
    with pytest.raises(api.ClawChatApiError) as error:
        http._auth_refresh_sync(REFRESH, 'test-device')
    assert error.value.code == code
    assert error.value.message.startswith('rejected')
    http.auth_refresh = AsyncMock(side_effect=error.value)
    logout = AsyncMock(return_value=True)
    manager = RefreshManager(build_client=lambda: http, persist_tokens=AsyncMock(),
                             persist_logout=logout, device_id_provider=lambda: 'test-device',
                             max_transient_retries=0)
    result = await manager.refresh(access_token=ACCESS, refresh_token=REFRESH)
    assert result.status == ('permanent' if code in (10003, 400) else 'transient')
    for secret in (REFRESH, ACCESS, JWT, OPAQUE):
        assert secret not in str(error.value)
        assert secret not in caplog.text
        assert secret not in (result.error or '')
        assert secret not in str(logout.call_args)
    assert 'kind=' in caplog.text and 'status=' in caplog.text and f'code={code}' in caplog.text


@pytest.mark.parametrize('exception', [URLError(MESSAGE), TimeoutError(MESSAGE), OSError(MESSAGE)])
def test_transport_exception_is_redacted_at_source(monkeypatch, exception):
    def fail(*_args, **_kwargs):
        raise exception
    monkeypatch.setattr(api, 'urlopen', fail)
    with pytest.raises(api.ClawChatApiError) as error:
        client()._auth_refresh_sync(REFRESH, 'test-device')
    for secret in (REFRESH, ACCESS, JWT, OPAQUE):
        assert secret not in str(error.value)
        assert secret not in ''.join(traceback.format_exception(error.value))


async def test_retry_log_redacts_even_injected_client_error(caplog):
    http = SimpleNamespace(_token=ACCESS, auth_refresh=AsyncMock(side_effect=[
        api.ClawChatApiError('api', MESSAGE, code=1), api.RefreshResult('new-access', 'new-refresh')]))
    result = await api.auth_refresh_with_retry(http, refresh_token=REFRESH, device_id='test-device',
                                               sleep=AsyncMock())
    assert result.access_token == 'new-access'
    for secret in (REFRESH, ACCESS, JWT, OPAQUE):
        assert secret not in caplog.text


def test_whitespace_length_and_known_short_secret():
    text = api.redact_refresh_message(' a\n b\tshort-placeholder ' + 'word ' * 100, 'short-placeholder')
    assert '\n' not in text and '\t' not in text and len(text) == 200
    assert 'short-placeholder' not in text and '***' in text


def test_short_padded_jwt_is_redacted():
    assert api.redact_refresh_message('rejected eyJh.YQ==.Yg==') == 'rejected ***'
