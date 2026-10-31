"""Remote refresh diagnostics cannot carry credentials into exceptions or logs."""
import base64
import io
import json
import time
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock
from http.client import InvalidURL
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


@pytest.mark.parametrize('userinfo', [
    'placeholder-user:placeholder-pass', 'u:p', 'placeholder-user:p%40ss',
])
def test_refresh_message_strips_url_userinfo(userinfo):
    message = f'<urlopen error opening https://{userinfo}@example.com/v1/auth/refresh>'  # redact-allow:email
    text = api.redact_refresh_message(message)
    assert text == '<urlopen error opening https://example.com/v1/auth/refresh>'
    assert userinfo not in text
    assert 'placeholder-user' not in text and 'placeholder-pass' not in text



@pytest.mark.parametrize('userinfo', [
    "placeholder-user:place'holder", 'placeholder-user:place"holder',
    'placeholder-user:place`holder', 'placeholder-user:place<holder',
    'placeholder-user:place>holder', "placeholder-user:place@hold'er",
])
def test_refresh_message_strips_url_userinfo_containing_delimiters(userinfo):
    text = api.redact_refresh_message(f'failed https://{userinfo}@example.com/v1/auth/refresh')  # redact-allow:email
    assert text == 'failed https://example.com/v1/auth/refresh'
    assert 'placeholder-user' not in text and 'holder' not in text



def test_long_url_userinfo_is_still_removed():
    password = 'placeholder-' + 'p' * 5_000
    text = api.redact_refresh_message(f'failed https://placeholder-user:{password}@example.com/v1')  # redact-allow:email
    assert text == 'failed https://example.com/v1'
    assert 'placeholder-' not in text

@pytest.mark.parametrize('text', [
    'a' * 100_000, 'a' * 100_000 + '://', "a://'" * 25_000, 'https://' + 'a' * 100_000,
    'https://' + 'u:' + 'p' * 100_000 + '@example.com/',  # redact-allow:email
    'a://x@' * 20_000, '"https://example.com",' * 20_000,
])
def test_url_userinfo_stripping_is_linear_on_long_text(text):
    started = time.perf_counter()
    api.redact_refresh_message(text)
    api.strip_url_userinfo(text)
    assert time.perf_counter() - started < 2.0


@pytest.mark.parametrize('text, expected', [
    ('["https://example.com","https://placeholder-user:placeholder-pass@example.com/"]',  # redact-allow:email
     '["https://example.com","https://example.com/"]'),
    ('["https://placeholder-user:placeholder-pass@example.com","https://u2:p2@example.org/x"]',  # redact-allow:email
     '["https://example.com","https://example.org/x"]'),
    ('https://example.com/next?u=https://placeholder-user:placeholder-pass@example.org/',  # redact-allow:email
     'https://example.com/next?u=https://example.org/'),
])
def test_url_userinfo_is_removed_from_adjacent_and_embedded_urls(text, expected):
    assert api.strip_url_userinfo(text) == expected
    assert 'placeholder-pass' not in api.redact_refresh_message(text)

@pytest.mark.parametrize('authority', [
    'u:p@example.com:99999x', 'u:p@example.com:99999', 'u:p@[example.com',  # redact-allow:email
])
def test_refresh_message_redacts_unparseable_url(authority):
    text = api.redact_refresh_message(f'failed https://{authority}/')
    assert text == 'failed [redacted-url]/'
    assert 'u:p' not in text


def test_url_without_userinfo_is_unchanged():
    text = 'HTTPS://Example.com:443/v1/auth/refresh?q=placeholder#placeholder'
    assert api.strip_url_userinfo(text) == text
    short = 'https://example.com/v1/auth/refresh'
    assert api.redact_refresh_message(short) == short
    assert api.strip_url_userinfo('https://example.com/placeholder@placeholder') == (
        'https://example.com/placeholder@placeholder')


@pytest.mark.parametrize('delimiter', [' ', '\n', '"', "'", '<', '>', '`'])
def test_url_userinfo_stripping_preserves_delimiters_and_url_parts(delimiter):
    text = f'https://u:p@example.com:8443/path?q=placeholder#placeholder{delimiter}suffix'  # redact-allow:email
    assert api.strip_url_userinfo(text) == (
        f'https://example.com:8443/path?q=placeholder#placeholder{delimiter}suffix')


def test_url_userinfo_is_removed_before_secret_replacement_and_truncation():
    text = api.redact_refresh_message(
        ' https://placeholder-user:placeholder-pass@example.com/\n' + 'word ' * 100,  # redact-allow:email
        'https://',
    )
    assert text.startswith('***example.com/ word ')
    assert len(text) == 200 and '\n' not in text
    assert 'placeholder-user' not in text and 'placeholder-pass' not in text


@pytest.mark.parametrize('password', ['placeholder-pass', 'placeholder pass with spaces', 'placeholder%20pass'])
@pytest.mark.parametrize('make_error', ['url', 'authority', 'port'])
async def test_url_transport_error_is_withheld_in_exception_outcome_and_log(
        monkeypatch, caplog, password, make_error):
    http = api.ClawChatApiClient(
        base_url=f'https://placeholder-user:{password}@example.com',  # redact-allow:email
        token=ACCESS, device_id='placeholder-device',
    )

    def fail(request, **_kwargs):
        authority = request.full_url.split('://', 1)[1].split('/', 1)[0]
        if make_error == 'url':
            raise URLError(f'opening {request.full_url}')
        if make_error == 'authority':
            raise InvalidURL(f"nonnumeric port: '{authority}'")
        raise InvalidURL(f"nonnumeric port: '{request.host.rsplit(':', 1)[-1]}'")

    monkeypatch.setattr(api, 'urlopen', fail)
    with pytest.raises(api.ClawChatApiError) as error:
        http._auth_refresh_sync(REFRESH, 'placeholder-device')
    assert error.value.message == api.DIAGNOSTIC_WITHHELD
    http.auth_refresh = AsyncMock(side_effect=error.value)
    manager = RefreshManager(
        build_client=lambda: http, persist_tokens=AsyncMock(), persist_logout=AsyncMock(),
        device_id_provider=lambda: 'placeholder-device', max_transient_retries=0,
    )
    result = await manager.refresh(access_token=ACCESS, refresh_token=REFRESH)
    assert result.status == 'transient'
    for output in (str(error.value), result.error, caplog.text):
        assert 'placeholder-user' not in output and 'placeholder' + '-pass' not in output
        assert 'placeholder pass' not in output and 'pass with spaces' not in output


@pytest.mark.parametrize('code,status', [(10003, 200), (10003, 401), (400, 200), (1, 200)])
def test_server_message_is_withheld_for_credentialed_base_url(monkeypatch, code, status):
    body = json.dumps({'code': code, 'msg': 'bad u:placeholder pass@example.com'}).encode()  # redact-allow:email

    def urlopen(request, **_kwargs):
        if status != 200:
            raise HTTPError(request.full_url, status, 'placeholder', {}, Response(body))
        return Response(body)

    monkeypatch.setattr(api, 'urlopen', urlopen)
    http = api.ClawChatApiClient(
        base_url='https://u:placeholder pass@example.com',  # redact-allow:email
        token=ACCESS, device_id='placeholder-device',
    )
    with pytest.raises(api.ClawChatApiError) as error:
        http._auth_refresh_sync(REFRESH, 'placeholder-device')
    assert error.value.code == code
    assert 'placeholder pass' not in str(error.value)
    assert 'placeholder pass' not in error.value.message


# Every counterexample from the earlier exact-match designs: with credentials in
# the configured base URL, only the fixed text is left.
CREDENTIALED_ECHOES = [
    ('https://u:placeholder pass@example.com', "nonnumeric port: 'u:placeholder pass@example.com'"),  # redact-allow:email
    ('https://u:placeholder pass@example.com', "nonnumeric port: 'placeholder pass@example.com'"),  # redact-allow:email
    ('https://u:placeholder%20pass@example.com', "nonnumeric port: 'placeholder pass@example.com'"),  # redact-allow:email
    ('https://u:place:holder:pass@example.com', "nonnumeric port: 'holder:pass@example.com'"),  # redact-allow:email
    ('https://u:place\\"holder pass@example.com', 'raw error place\\"holder pass@example.com'),  # redact-allow:email
    ('https://u:placeholder pass"' + 'x' * 220 + '@example.com',  # redact-allow:email
     json.dumps({'message': 'https://u:placeholder pass"' + 'x' * 220 + '@example.com'})),  # redact-allow:email
    ('https://u:access-placeholder@example.com',  # redact-allow:email
     'rejected placeholder-refresh-prefix-access-placeholder-private-tail here'),
    ('https://u:placeholder-pass%40@example.com',  # redact-allow:email
     'failed https://other-user:other-secret-placeholder-pass@example.org/v1'),  # redact-allow:email
    ('https://u:aaaa%3Abbbb@example.com', "nonnumeric port: 'bbbb@example.com'"),  # redact-allow:email
    ('https://u:abcd!:xy@example.com', 'bad abcd!%3Axy here'),  # redact-allow:email
    ('https://u:' + ':' * 50_000 + '@example.com', 'x' * 50_000),  # redact-allow:email
    ('https://u:abcde@example.com', 'abcd!'),  # redact-allow:email
    ('https://u:abcd%2F@example.com', 'abcd%2f'),  # redact-allow:email
    ('https://placeholder-user@example.com', 'failed placeholder-user'),  # redact-allow:email
    ('https://u:p@ss/word@example.com', 'failed ss/word'),  # redact-allow:email
    ('http://u:p@[bad', 'failed p'),
]


@pytest.mark.parametrize('base_url, text', CREDENTIALED_ECHOES)
def test_credentialed_base_url_withholds_every_diagnostic(base_url, text):
    from clawchat_gateway.connection import ClawChatConnection

    assert api.url_has_userinfo(base_url)
    assert api.redact_refresh_message(text, REFRESH, base_url=base_url) == api.DIAGNOSTIC_WITHHELD
    connection = object.__new__(ClawChatConnection)
    connection._cfg = SimpleNamespace(token=ACCESS, base_url=base_url, websocket_url='wss://example.com/ws')
    assert connection._sanitize_secret_text(text) == api.DIAGNOSTIC_WITHHELD
    connection._cfg = SimpleNamespace(token=ACCESS, base_url='https://example.com', websocket_url=base_url)
    assert connection._sanitize_secret_text(text) == api.DIAGNOSTIC_WITHHELD


@pytest.mark.parametrize('base_url', [
    None, '', 'https://example.com', 'https://example.com:8443/a@b', 'https://example.com/v1?x=a@b#c@d',  # redact-allow:email
])
def test_base_url_without_userinfo_keeps_normal_redaction(base_url):
    assert not api.url_has_userinfo(base_url)
    assert api.redact_refresh_message(f'rejected {REFRESH}', REFRESH, base_url=base_url) == 'rejected ***'


def test_redaction_fails_closed():
    class Hostile:
        def __str__(self):
            raise RuntimeError('placeholder-boom')

    assert api.redact_refresh_message(Hostile()) == api.DIAGNOSTIC_REDACTION_FAILED
    from clawchat_gateway.connection import ClawChatConnection

    connection = object.__new__(ClawChatConnection)
    connection._cfg = SimpleNamespace(token=Hostile(), base_url='', websocket_url='')
    assert connection._sanitize_secret_text('secret text') == api.DIAGNOSTIC_REDACTION_FAILED


@pytest.mark.parametrize('base_url, text', [
    ('https://u:' + 'a:' * 100_000 + '@example.com', 'x' * 100_000),  # redact-allow:email
    ('https://example.com', 'https://' + 'u:' * 50_000 + '@' + 'a' * 50_000),
    (None, 'a://x@' * 20_000),
])
def test_withhold_or_redact_is_fast_on_adversarial_input(base_url, text):
    started = time.perf_counter()
    api.redact_refresh_message(text, REFRESH, base_url=base_url)
    assert time.perf_counter() - started < 2.0


@pytest.mark.parametrize('password', ['placeholder-pass', "placeholder-pass`<x>@y'z"])
@pytest.mark.parametrize('token', ['', ACCESS])
def test_connection_diagnostic_strips_url_userinfo(token, password):
    from clawchat_gateway.connection import ClawChatConnection

    connection = object.__new__(ClawChatConnection)
    connection._cfg = SimpleNamespace(token=token, base_url='https://example.com', websocket_url='wss://example.com/ws')
    text = connection._sanitize_secret_text(
        f'https://placeholder-user:{password}@example.com/ {ACCESS}')  # redact-allow:email
    assert 'https://example.com/' in text
    assert 'placeholder-user' not in text and 'placeholder-pass' not in text
    assert password not in text
    assert (ACCESS not in text) == bool(token)
    assert connection._sanitize_secret_text(None) is None


@pytest.mark.parametrize('password', ['placeholder-pass', 'placeholder pass with spaces'])
async def test_activation_stale_user_warning_withholds_credentialed_base_url(monkeypatch, caplog, password):
    from clawchat_gateway import activate as activation

    monkeypatch.setattr(activation, '_load_config', lambda: (None, {}))
    monkeypatch.setattr(activation, '_read_existing_user_id',
                        lambda *_args, **_kwargs: 'placeholder-stale-identity')
    monkeypatch.setattr(activation, '_resolve_activation_device_id',
                        lambda **_kwargs: 'placeholder-device')
    monkeypatch.setattr(activation, '_identity_is_this_profiles_own', lambda _value: True)
    monkeypatch.setattr(activation, 'get_device_id', lambda: 'placeholder-device')
    monkeypatch.setattr(activation, 'onboarding_context', lambda: {})
    monkeypatch.setattr(api.ClawChatApiClient, 'agents_connect_check', AsyncMock(return_value=None))
    stop = api.ClawChatApiError('validation', 'placeholder-stop')
    connect = AsyncMock(side_effect=[
        api.ClawChatApiError('api', 'placeholder-missing', code=api.AGENT_NOT_FOUND_CODE), stop,
    ])
    monkeypatch.setattr(activation, 'agents_connect_with_retry', connect)
    with pytest.raises(api.ClawChatApiError) as error:
        await activation.activate(
            'placeholder-code', repair=True,
            base_url=f'https://placeholder-user:{password}@example.com',  # redact-allow:email
        )
    assert error.value is stop and connect.await_count == 2
    assert 'no longer exists on (base URL withheld: contains credentials)' in caplog.text
    assert 'placeholder-user' not in caplog.text and password not in caplog.text
