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
    api.redact_configured_secrets(text, 'https://u:aaaa@example.com')  # redact-allow:email
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


@pytest.mark.parametrize('text, expected', [
    ('opening https://placeholder-user:placeholder pass@example.com/v1',  # redact-allow:email
     'opening https://***@example.com/v1'),
    ("nonnumeric port: 'placeholder pass@example.com'", "nonnumeric port: '***@example.com'"),  # redact-allow:email
    ("nonnumeric port: 'placeholder-user:placeholder pass@example.com'",  # redact-allow:email
     "nonnumeric port: '***@example.com'"),
    ('bad placeholder%20pass here', 'bad *** here'),
    ('cut placeholder pa', 'cut ***'),
])
def test_configured_secrets_are_removed_anywhere(text, expected):
    base_url = 'https://placeholder-user:placeholder pass@example.com/'  # redact-allow:email
    assert api.redact_configured_secrets(text, base_url, None) == expected
    assert api.configured_secret_forms('https://example.com/a@b') == ()
    assert api.configured_secret_forms('https://example.com:8443/a@b') == ()
    assert api.configured_secret_forms(None) == ()


@pytest.mark.parametrize('password, text', [
    # http.client echoes the whole authority without a scheme.
    ('placeholder pass', "nonnumeric port: 'u:placeholder pass@example.com'"),  # redact-allow:email
    # urllib decodes %20 before echoing the host.
    ('placeholder%20pass', "nonnumeric port: 'placeholder pass@example.com'"),  # redact-allow:email
    ('placeholder%20pass', 'opening https://u:placeholder%20pass@example.com/'),  # redact-allow:email
    # A password with colons: only the part after the last colon is echoed.
    ('place:holder:pass', "nonnumeric port: 'holder:pass@example.com'"),  # redact-allow:email
    ('place:holder-pass', "nonnumeric port: 'holder-pass@example.com'"),  # redact-allow:email
    # Backslashes and quotes, raw, JSON-escaped and repr-escaped.
    ('place\\holder"pass', 'raw place\\holder"pass@example.com'),  # redact-allow:email
    ('place\\holder"pass', '{"msg":"bad place\\\\holder\\"pass@example.com"}'),  # redact-allow:email
    ("place\\holder'pass", "error('bad place\\\\holder\\'pass@example.com')"),  # redact-allow:email
    ('place holder"pass', '{"msg":"bad https://u:place holder\\"pass@example.com/"}'),  # redact-allow:email
    # Percent-encoded forms of a raw password.
    ('place holder/pass', 'opening https://u:place%20holder%2Fpass@example.com/'),  # redact-allow:email
    # A diagnostic truncated inside the password.
    ('placeholder pass' + 'x' * 220, ('{"msg":"https://u:placeholder pass' + 'x' * 220)[:120]),
])
def test_configured_secret_forms_never_leak(password, text):
    base_url = f'https://u:{password}@example.com'  # redact-allow:email
    secret = password.replace('%20', ' ')
    redacted = api.redact_configured_secrets(text, base_url)
    message = api.redact_refresh_message(redacted)
    for output in (redacted, message):
        for fragment in {secret[:4], secret[-4:], 'holder', secret.rsplit(':', 1)[-1][:4]}:
            assert fragment not in output, (fragment, output)


@pytest.mark.parametrize('text, expected', [
    ('failed https://other-user:other-placeholder-pass@example.org/v1',  # redact-allow:email
     'failed https://example.org/v1'),
    ('failed https://placeholder-user:placeholder-pass@example.com/v1 and '  # redact-allow:email
     'https://other-user:other-placeholder-pass@example.org/',  # redact-allow:email
     'failed https://example.com/v1 and https://example.org/'),
    ('metadata-placeholder-pass@x', 'metadata-***@x'),  # redact-allow:email
    ("failed https://other-user:other-secret'placeholder-pass@example.org/v1",  # redact-allow:email
     'failed https://example.org/v1'),
    ("refused: 'placeholder-pass@example.com'", "refused: '***@example.com'"),  # redact-allow:email
])
def test_configured_secret_removal_then_url_scan(text, expected):
    base_url = 'https://placeholder-user:placeholder-pass@example.com'  # redact-allow:email
    assert api.strip_url_userinfo(api.redact_configured_secrets(text, base_url)) == expected


@pytest.mark.parametrize('text, password', [
    ('@' * 100_000, '@' * 5_000),
    ("'" + 'a' * 99_999, 'a' * 5_000),
    ('ab' * 50_000, 'ab' * 2_500 + 'c'),
])
def test_configured_secret_removal_is_linear(text, password):
    started = time.perf_counter()
    api.redact_configured_secrets(text, f'https://u:{password}@example.com')  # redact-allow:email
    assert time.perf_counter() - started < 2.0

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


def _url_error(request):
    return URLError(f'opening {request.full_url}')


def _host_error(request):
    return InvalidURL(f"nonnumeric port: '{request.host.rsplit(':', 1)[1]}'")


def _authority_error(request):
    return InvalidURL(f"nonnumeric port: '{request.full_url.split('://', 1)[1].split('/', 1)[0]}'")


@pytest.mark.parametrize('password, make_error', [
    ('placeholder-pass', _url_error),
    ('placeholder pass with spaces', _url_error),
    ('placeholder pass with spaces', _host_error),
    ('placeholder-pass', _host_error),
    ('placeholder%20pass', _host_error),
    ('place:holder-pass', _host_error),
    ('placeholder pass with spaces', _authority_error),
    ('placeholder%20pass', _authority_error),
])
async def test_url_transport_error_is_redacted_in_exception_outcome_and_log(
        monkeypatch, caplog, password, make_error):
    http = api.ClawChatApiClient(
        base_url=f'https://placeholder-user:{password}@example.com',  # redact-allow:email
        token=ACCESS, device_id='placeholder-device',
    )

    def fail(request, **_kwargs):
        raise make_error(request)

    monkeypatch.setattr(api, 'urlopen', fail)
    with pytest.raises(api.ClawChatApiError) as error:
        http._auth_refresh_sync(REFRESH, 'placeholder-device')
    http.auth_refresh = AsyncMock(side_effect=error.value)
    manager = RefreshManager(
        build_client=lambda: http, persist_tokens=AsyncMock(), persist_logout=AsyncMock(),
        device_id_provider=lambda: 'placeholder-device', max_transient_retries=0,
    )
    result = await manager.refresh(access_token=ACCESS, refresh_token=REFRESH)
    assert result.status == 'transient'
    for output in (str(error.value), result.error, caplog.text):
        assert 'example.com' in output
        assert 'placeholder-user' not in output
        assert password not in output and 'pass with spaces' not in output
        assert 'placeholder pass' not in output and 'holder-pass' not in output


@pytest.mark.parametrize('password', [
    'placeholder-pass', "placeholder-pass`<x>@y'z", 'placeholder pass with spaces',
])
@pytest.mark.parametrize('token', ['', ACCESS])
def test_connection_diagnostic_strips_url_userinfo(token, password):
    from clawchat_gateway.connection import ClawChatConnection

    base_url = f'https://placeholder-user:{password}@example.com'  # redact-allow:email
    connection = object.__new__(ClawChatConnection)
    connection._cfg = SimpleNamespace(token=token, base_url=base_url, websocket_url='wss://example.com/ws')
    text = connection._sanitize_secret_text(f'{base_url}/ {ACCESS}')
    assert 'https://example.com/' in text
    assert 'placeholder-user' not in text and 'placeholder-pass' not in text
    assert password not in text
    assert (ACCESS not in text) == bool(token)
    assert connection._sanitize_secret_text(None) is None


@pytest.mark.parametrize('password', ['placeholder-pass', 'placeholder pass with spaces'])
async def test_activation_stale_user_warning_strips_url_userinfo(monkeypatch, caplog, password):
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
    assert 'no longer exists on https://example.com' in caplog.text
    assert 'placeholder-user' not in caplog.text and password not in caplog.text
