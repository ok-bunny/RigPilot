"""Canonical spelling without changing legacy Worker value semantics."""
from __future__ import annotations

import pytest

from server.miner_adapters.templates import (
    DEFAULT_WALLET_WORKER_TEMPLATE, DEFAULT_WORKER_TEMPLATE,
    MAX_WORKER_TEMPLATE_CHARS,
    normalize_worker_name_template, render_worker_name_template,
    validate_worker_name_template, worker_template_uses_agent_name,
)


def render(template, *, name='Visible-rig', hostname='os-host', agent_id='agent-123'):
    return render_worker_name_template(template, agent_name=name, system_hostname=hostname, agent_id=agent_id)


@pytest.mark.parametrize('token', ['%WORKER_NAME%', '%WORK_NAME%', '%worker_name%', '%Work_Name%', '{hostname}', '{agent_name}'])
def test_legacy_worker_aliases_have_one_standard_spelling_and_visible_name_meaning(token):
    template = 'prefix-' + token + '-suffix'
    assert normalize_worker_name_template(template) == 'prefix-%WORKER_NAME%-suffix'
    assert validate_worker_name_template(template) == 'prefix-%WORKER_NAME%-suffix'
    assert render(template) == render(normalize_worker_name_template(template)) == 'prefix-Visible-rig-suffix'
    assert worker_template_uses_agent_name(template)


def test_new_defaults_and_rigpilot_only_tokens_keep_distinct_meanings():
    assert DEFAULT_WORKER_TEMPLATE == '%WORKER_NAME%'
    assert DEFAULT_WALLET_WORKER_TEMPLATE == '%WAL%.%WORKER_NAME%'
    raw = '{system_hostname}-{agent_id}'
    assert normalize_worker_name_template(raw) == raw
    assert render(raw) == 'os-host-agent-123'
    assert render(raw, name='Renamed-rig') == 'os-host-agent-123'
    assert not worker_template_uses_agent_name(raw)
    assert render(DEFAULT_WORKER_TEMPLATE, name='Renamed-rig') == 'Renamed-rig'


@pytest.mark.parametrize('template', ['{hostname}', '{agent_name}', '%WORK_NAME%', '%WORKER_NAME%'])
def test_inserted_visible_name_is_literal_across_worker_stages(template):
    name = 'Rig%WAL%{hostname}%WORKER_NAME%'
    worker = render('pre-' + template, name=name)
    assert worker == 'pre-' + name


def test_old_near_limit_template_remains_editable_after_canonical_expansion():
    old = '{hostname}' * 12
    assert len(old) == 120
    canonical = validate_worker_name_template(old)
    assert len(canonical) == 156 > 128
    assert len(canonical) <= MAX_WORKER_TEMPLATE_CHARS
    assert render(old, name='r') == render(canonical, name='r') == 'r' * 12
    assert validate_worker_name_template(canonical) == canonical
    with pytest.raises(ValueError, match='rendered worker name'):
        render(canonical, name='long-visible-device')


@pytest.mark.parametrize('raw', ['literal WORK_NAME WORKER_NAME hostname', '%UNKNOWN%', '%WAL%', '%20%32', '%workKer_name%'])
def test_unrecognized_percent_or_bare_words_are_not_invented_worker_placeholders(raw):
    assert normalize_worker_name_template(raw) == raw
    assert render(raw) == raw
    assert not worker_template_uses_agent_name(raw)


@pytest.mark.parametrize('raw', ['{unknown}', '{wallet}', '{{hostname}}', 'name\nother', '', 'x' * 513])
def test_worker_input_validation_retains_brace_and_control_boundaries(raw):
    with pytest.raises(ValueError):
        validate_worker_name_template(raw)


def test_worker_name_output_budget_and_control_chars_are_still_enforced():
    assert render('%WORKER_NAME%', name='x' * 128) == 'x' * 128
    for name in ('x' * 129, 'bad\nname', '   '):
        with pytest.raises(ValueError):
            render('%WORKER_NAME%', name=name)
