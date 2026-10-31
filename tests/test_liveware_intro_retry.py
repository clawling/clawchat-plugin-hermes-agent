"""Owner intro retries promptly, stays bounded, and stops for stale instances."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from clawchat_gateway import liveware_sample as sample, tools


@pytest.fixture
def supervisor(tmp_path):
    deps = sample.LivewareSampleDeps(
        platform='clawchat', account_id='test-agent', enabled=True, store=SimpleNamespace(
            mark_liveware_sample_intro_sent=Mock()), sample_root=tmp_path,
        resolve_token=lambda: 'placeholder-access', resolve_liveware_path=lambda: 'placeholder-cli',
        list_apps=AsyncMock(), register_app=AsyncMock(), notify_owner=AsyncMock(return_value=False))
    return sample.LivewareSampleSupervisor(deps)


@pytest.mark.parametrize('succeed_at', [None, 4])
async def test_retry_ladder_and_unchanged_attempt_limit(supervisor, succeed_at):
    pending = []
    times, delays = [], []
    clock = [0.0]
    async def sleep(seconds):
        delays.append(seconds)
        clock[0] += seconds
    async def notify(_text):
        times.append(clock[0])
        return len(times) == succeed_at
    supervisor._d.intro_sleep = sleep
    supervisor._d.notify_owner = notify
    supervisor._spawn_task = pending.append
    await supervisor._deliver_intro()
    while pending:
        await pending.pop(0)
    assert times[:4] == [0.0,2.0,7.0,17.0]
    assert delays[:3] == [2.0,5.0,10.0]
    if succeed_at:
        assert len(times) == succeed_at
        supervisor._d.store.mark_liveware_sample_intro_sent.assert_called_once()
    else:
        assert len(times) == sample._INTRO_MAX_TRIES == 20
        assert delays[3:] == [30.0] * 16
        assert times[-1] <= 30.0 * (sample._INTRO_MAX_TRIES - 1)
        supervisor._d.store.mark_liveware_sample_intro_sent.assert_not_called()


@pytest.mark.parametrize('transition', ['stop','replace'])
async def test_late_intro_retry_is_ignored(supervisor, transition):
    async def sleep(_seconds):
        if transition == 'stop':
            supervisor._stopped = True
        else:
            supervisor._generation += 1
    supervisor._d.intro_sleep = sleep
    await supervisor._retry_intro(1)
    supervisor._d.notify_owner.assert_not_called()


async def test_late_delivery_cannot_mark_replaced_instance(supervisor):
    async def notify(_text):
        supervisor._generation += 1
        return True
    supervisor._d.notify_owner = notify
    await supervisor._deliver_intro()
    supervisor._d.store.mark_liveware_sample_intro_sent.assert_not_called()


async def test_login_error_explains_both_locations_and_verification(monkeypatch):
    monkeypatch.setattr(tools, 'load_profile_config', lambda: SimpleNamespace(
        token='placeholder-access', agent_id='test-agent'))
    monkeypatch.setattr(tools, 'resolve_liveware_path', lambda: None)
    monkeypatch.setattr(tools.asyncio, 'create_subprocess_exec', AsyncMock(side_effect=AssertionError('unexpected spawn')))
    result = await tools.liveware_login()
    assert 'error' in result
    text = str(result)
    assert 'PATH' in text and '<HERMES_HOME>/clawchat/liveware' in text
    assert 'running' in text and 'failed verification' in text and 'gateway log' in text


def test_skill_lists_registered_apps_before_create():
    text = (Path(__file__).resolve().parents[1] / 'skills/clawchat-liveware/SKILL.md').read_text()
    procedure = text.split('## Procedure', 1)[1]
    assert procedure.index('clawchat_list_apps()') < procedure.index('app create')
    assert 'Reuse an app' in procedure and 'instead of creating' in procedure


async def test_scheduled_intro_retry_keeps_its_original_generation(supervisor):
    pending = []
    supervisor._spawn_task = pending.append
    supervisor._d.intro_sleep = AsyncMock()
    await supervisor._deliver_intro()
    assert len(pending) == 1
    supervisor._generation += 1
    await pending.pop()
    supervisor._d.intro_sleep.assert_not_called()
    assert supervisor._d.notify_owner.await_count == 1
