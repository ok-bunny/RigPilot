"""Existing native SRB flight replacement keeps cleared OC out of saved/restarted argv."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from agent.commands import Command, CommandExecutor
from agent.managed_miners import ManagedMinerManager, build_srbminer_argv
from agent.tests.test_managed_mining_commands import config, srb_payload


@pytest.mark.parametrize('clear_all', [False, True])
def test_srb_flight_clear_persists_complete_target_and_restart_cannot_readd(tmp_path, monkeypatch, clear_all):
    monkeypatch.setattr('agent.commands.platform.system', lambda: 'Linux')
    cfg = config(tmp_path, allow_gpu_oc=True)
    launches = []
    def capture(document):
        doc = copy.deepcopy(document)
        argv = build_srbminer_argv(doc, executable=tmp_path/'SRBMiner-MULTI', api_port=23456, log_path=tmp_path/'miner.log')
        launches.append((doc, argv))
        return {'managed': True, 'miner_type': 'srbminer', 'pid': 123}
    def installer(metadata, _config, **_kwargs):
        path = tmp_path/'packages'/'srb';path.mkdir(parents=True, exist_ok=True)
        return {'package_id': metadata['id'], 'version': metadata['version'], 'path': str(path), 'sha256': None, 'downloaded': True}
    def forbidden(*_args, **_kwargs):pytest.fail('no real miner, vendor OC or process execution')
    executor = CommandExecutor(cfg, runner=forbidden, package_installer=installer,
        gpu_backend_factory=forbidden, managed_miner_factory=lambda _cfg: SimpleNamespace(apply=capture),)
    before = srb_payload()
    first = executor.execute(Command('srb-before-clear', 'apply_flight_sheet', before))
    assert first.status == 'succeeded', first.error
    assert '--gpu-moffset0' in launches[-1][1]

    after = copy.deepcopy(before)
    if clear_all:after['gpu_oc'] = None
    else:after['gpu_oc']['settings'][0].pop('memory_clock_offset_mhz')
    outcome = executor.execute(Command('srb-after-clear', 'apply_flight_sheet', after))
    assert outcome.status == 'succeeded', outcome.error
    saved = json.loads(cfg.flight_sheet_file.read_text())
    assert saved['gpu_oc'] == after['gpu_oc']
    assert '_reset_srb_gpu_oc' not in saved
    assert '--gpu-reset-oc' in launches[-1][1]
    assert '--gpu-moffset0' not in launches[-1][1]
    if not clear_all:
        assert launches[-1][1][launches[-1][1].index('--gpu-plimit0')+1] == '120'
        assert launches[-1][1][launches[-1][1].index('--gpu-coffset0')+1] == '100'
    else:
        assert not any(flag in launches[-1][1] for flag in ['--gpu-plimit0','--gpu-coffset0'])

    manager = ManagedMinerManager(cfg, system_name='Linux')
    monkeypatch.setattr(manager, 'apply', capture)
    assert manager.restart()['restarted'] is True
    assert ('--gpu-reset-oc' in launches[-1][1]) is (not clear_all)
    assert '--gpu-moffset0' not in launches[-1][1]
    assert launches[-1][0]['gpu_oc'] == after['gpu_oc']
    assert json.loads(cfg.flight_sheet_file.read_text())['gpu_oc'] == after['gpu_oc']
