"""Behavior cases for shared rules and Controller input boundaries."""
from dataclasses import replace
import pytest
from test_059_backend import app_client, login, auth
from server.miner_adapters import MINER_ADAPTERS, MinerAdapterRegistry
from server.miner_adapters.validation import validate_option_value, parse_pool_endpoint
from server.main import _normalize_managed_options
from server.managed_miners import normalize_algorithm

@pytest.mark.parametrize("value", [True, False, 1.0, "1"])
def test_integer_choice_refuses_other_scalar_types(value):
    spec = next(s for s in MINER_ADAPTERS.require("srbminer").options if s.name == "cpu_threads_intensity")
    with pytest.raises(ValueError): validate_option_value(spec, value)
    with pytest.raises(Exception) as rejected:
        _normalize_managed_options(miner_type="srbminer", coin="XMR", algorithm="randomx", pool_url="stratum+tcp://pool.test:3333", extra_config={spec.name: value})
    assert getattr(rejected.value, "status_code", None) == 422

@pytest.mark.parametrize("value", [1, 2, 4])
def test_integer_choice_preserves_valid_wire(value):
    algo, options = _normalize_managed_options(miner_type="srbminer", coin="XMR", algorithm="rx/0", pool_url="stratum+ssl://pool.test:443", extra_config={"cpu_threads_intensity": value})
    assert algo == "randomx" and options["cpu_threads_intensity"] == value and type(options["cpu_threads_intensity"]) is int
    assert options["tls"] is True

def test_srb_coin_constraint_matches_shared_contract():
    with pytest.raises(ValueError, match="Unsupported SRBMiner algorithm"):
        normalize_algorithm("srbminer", "PEARL", "randomx")
    assert normalize_algorithm("srbminer", "XMR", "rx") == "randomx"

@pytest.mark.parametrize("url", ["stratum+tcp://pool.test:3333\n", "stratum+tcp://pool.te\nst:3333", "stratum+tcp://pool\t.test:3333", "stratum+tcp://pool .test:3333", "stratum+tcp://user:secret@pool.test:3333", "stratum+tcp://pool.test:3333/path", "stratum+tcp://pool.test:70000"])
def test_pool_raw_boundary_rejects_malformed_at_saved_and_flight_schema(app_client, url):
    from server.schemas import FlightMinerCreate
    with pytest.raises(ValueError): parse_pool_endpoint(url, {"stratum+tcp"})
    if any(ord(c) < 32 for c in url) or "pool .test" in url:
        with pytest.raises(ValueError): FlightMinerCreate(miner_type="generic", coin="XMR", wallet_id="w", pool_url=url)
    client, _ = app_client
    response = client.post("/api/pools", headers=auth(login(client)), json={"name":"bad", "coin":"XMR", "pool_url":url})
    assert response.status_code == 422
    flight = client.post("/api/flight-sheets", headers=auth(login(client)), json={"name":"bad endpoint", "miner_type":"generic", "coin":"XMR", "wallet_id":"w", "pool_url":url})
    assert flight.status_code == 422

@pytest.mark.parametrize("url,host,port,tls", [(" stratum+tcp://127.0.0.1:3333 ","127.0.0.1",3333,False),("stratum+ssl://[::1]:443","::1",443,True),("pearl://pool.test:443","pool.test",443,False)])
def test_endpoint_retains_valid_url_semantics(url, host, port, tls):
    endpoint = parse_pool_endpoint(url, {"stratum+tcp", "stratum+ssl", "pearl"})
    assert (endpoint.url,endpoint.host,endpoint.port,endpoint.tls) == (url.strip(),host,port,tls)

def test_nullable_option_remains_omitted_on_controller_wire():
    _, options = _normalize_managed_options(miner_type="srbminer", coin="XMR", algorithm="randomx", pool_url="stratum+tcp://pool.test:3333", extra_config={"cpu_threads_intensity":None})
    assert "cpu_threads_intensity" not in options


@pytest.mark.parametrize("key,value", [
    ("cpu_threads", True), ("cpu_threads", 0), ("cpu_threads", 1025),
    ("cpu_threads_intensity", True), ("cpu_threads_intensity", 1.0),
    ("tls_cert_sha256", ""), ("tls_cert_sha256", "x" * 513),
    ("tls_cert_sha256", "pin\nwith-control"),
])
def test_manifest_defaults_follow_the_same_scalar_boundaries_as_job_options(key, value):
    adapter = MINER_ADAPTERS.require("srbminer")
    spec = next(item for item in adapter.options if item.name == key)
    with pytest.raises(ValueError):
        validate_option_value(spec, value)
    options = tuple(replace(item, default=value) if item.name == key else item for item in adapter.options)
    with pytest.raises(ValueError):
        MinerAdapterRegistry((replace(adapter, options=options),))


from server.miner_adapters.validation import normalize_extra_args, parse_extra_args


@pytest.mark.parametrize('raw,expected', [
    ('--pearl-k1', ['--pearl-k1']),
    ('--pearl-k1 0,2\n--future-option=enabled', ['--pearl-k1', '0,2', '--future-option=enabled']),
    ('--future-option "a b" \'c d\'', ['--future-option', 'a b', 'c d']),
    ('--future-option "" \'\' --other=', ['--future-option', '', '', '--other=']),
    ('--label="abc def" --label=\'ghi jkl\'', ['--label=abc def', '--label=ghi jkl']),
    (r'--future-path "C:\Program Files\Miner\data.bin"', ['--future-path', r'C:\Program Files\Miner\data.bin']),
    (r'--future-path "\\server\share name\data.bin"', ['--future-path', r'\\server\share name\data.bin']),
    (r"--future-path 'C:\Program Files\Miner\'", ['--future-path', 'C:\\Program Files\\Miner\\']),
    (r'--message "say \"hello\""', ['--message', 'say "hello"']),
    ('--future $(id) ; | > `whoami` %PATH%', ['--future', '$(id)', ';', '|', '>', '`whoami`', '%PATH%']),
    ('--future "line\none"', ['--future', 'line\none']),
])
def test_extra_args_become_literal_argv_on_every_native_miner(raw, expected):
    for kind in ('xmrig', 'srbminer', 'pearlminer'):
        assert parse_extra_args(raw, miner_type=kind) == expected


@pytest.mark.parametrize('raw', [None, '', '  \t\r\n'])
def test_explicit_extra_args_clear_uses_empty_wire_text_and_no_argv(raw):
    assert normalize_extra_args(raw) == ''
    assert parse_extra_args(raw) == []
    spec = next(s for s in MINER_ADAPTERS.require('srbminer').options if s.name == 'extra_args')
    assert validate_option_value(spec, raw) == ''


def test_extra_args_preserves_editable_text_and_normalizes_line_endings_only():
    raw = '  --future="a b"\r\n--pearl-k1 0,1  '
    assert normalize_extra_args(raw) == '  --future="a b"\n--pearl-k1 0,1  '
    spec = next(s for s in MINER_ADAPTERS.require('srbminer').options if s.name == 'extra_args')
    assert validate_option_value(spec, raw) == normalize_extra_args(raw)


@pytest.mark.parametrize('raw', ['--open "oops', "--open 'oops", '--bad\x00value', '--bad\x1bvalue', '--bad\x7fvalue', '--bad\x85value', 'x' * 4097, [], False, 1])
def test_extra_args_rejects_ambiguous_quotes_controls_and_invalid_wire(raw):
    with pytest.raises(ValueError):
        parse_extra_args(raw)


def test_extra_args_maximum_is_the_editable_character_budget():
    assert normalize_extra_args('x' * 4096) == 'x' * 4096


@pytest.mark.parametrize('raw', ['—pearl-k1', '–pearl-k1 0,1', '"—pearl-k1"'])
def test_unicode_option_dash_gives_a_specific_ASCII_correction(raw):
    with pytest.raises(ValueError, match='ASCII.*--pearl-k1'):
        parse_extra_args(raw, miner_type='srbminer')


@pytest.mark.parametrize('kind,raw', [
    ('xmrig', '--config=outside.json'), ('xmrig', '-coutside.json'),
    ('xmrig', '--url pool:1234'), ('xmrig', '-uwallet'),
    ('xmrig', '--http-port 9999'), ('xmrig', '--threads 4'),
    ('srbminer', '--wallet other'), ('srbminer', '-uother'),
    ('srbminer', '--algorithm-gpu qhash'), ('srbminer', '-ag qhash'),
    ('srbminer', '--gpu-id 0'), ('srbminer', '--gpu-cclock1=2000'),
    ('srbminer', '--GPU-PLIMIT0 100'), ('srbminer', '--gpu-disable-oc'),
    ('srbminer', '--log-file /tmp/outside.log'),
    ('srbminer', '--startup-script outside.sh'),
    ('srbminer', '--a0-oc-script=outside.sh'), ('srbminer', '--a1-oc-script outside.sh'),
    ('srbminer', '--reboot-script-gpu-watchdog outside.sh'),
    ('srbminer', '--api-rig-shutdown-url secret'),
    ('pearlminer', '--address other'), ('pearlminer', '--proxy pool:1234'),
    ('pearlminer', '--worker other'), ('pearlminer', '-gpu=0'),
])
def test_extra_args_refuses_existing_identity_hardware_API_and_path_overrides(kind, raw):
    with pytest.raises(ValueError, match='conflicts with managed parameter'):
        parse_extra_args(raw, miner_type=kind)


def test_extra_args_reserved_set_is_finite_and_unknown_future_flags_pass_through():
    raw = '--gpu-future-setting 1 --api-future-metric=on --pearl-k1 0,1 --future-value "--gpu-future-inner"'
    assert parse_extra_args(raw, miner_type='srbminer') == [
        '--gpu-future-setting', '1', '--api-future-metric=on', '--pearl-k1', '0,1', '--future-value', '--gpu-future-inner',
    ]
    with pytest.raises(ValueError, match='--instance-core'):
        parse_extra_args('--instance-core=1', reserved_flags={'--instance-core'})


def test_multiline_extra_args_does_not_widen_other_string_option_rules():
    pin = next(s for s in MINER_ADAPTERS.require('srbminer').options if s.name == 'tls_cert_sha256')
    with pytest.raises(ValueError, match='control characters'):
        validate_option_value(pin, 'pin\nother')


@pytest.mark.parametrize('raw', ['--pearl-k1', '--pearl-k1 0,2', '--pearl-k1=0,2'])
def test_known_K1_feature_refuses_explicit_older_binary_before_execution(raw):
    adapter = MINER_ADAPTERS.require('srbminer')
    with pytest.raises(ValueError, match='--pearl-k1 requires SRBMiner 3.7.2 or newer'):
        adapter.validate_execution_context('pearlhash', '3.4.6', {'extra_args': raw}, coin='PEARL')
    assert adapter.validate_execution_context('pearlhash', '3.7.2', {'extra_args': raw}, coin='PEARL') == 'pearlhash'


@pytest.mark.parametrize('raw', ['', '--future-option 1', '--gpu-future-setting=on', '--pearl-k999 0,1'])
def test_old_SRB_binary_without_known_K1_keeps_its_existing_and_future_flag_policy(raw):
    assert MINER_ADAPTERS.require('srbminer').validate_execution_context(
        'pearlhash', '3.4.6', {'extra_args': raw}, coin='PEARL') == 'pearlhash'


def test_K1_unresolved_version_is_checked_again_after_package_resolution():
    adapter = MINER_ADAPTERS.require('srbminer')
    options = {'extra_args': '--pearl-k1', 'resource_kind': 'gpu'}
    assert adapter.validate_execution_context('pearlhash', None, options, coin='PEARL') == 'pearlhash'
    with pytest.raises(ValueError, match='3.7.2'):
        adapter.validate_execution_context('pearlhash', '3.7.1', options, coin='PEARL')
    assert adapter.validate_execution_context('pearlhash', '3.7.2', options, coin='PEARL') == 'pearlhash'
    assert options == {'extra_args': '--pearl-k1', 'resource_kind': 'gpu'}


def test_execution_context_reuses_shared_identity_boundaries_and_algorithm_version_rules():
    adapter = MINER_ADAPTERS.require('srbminer')
    with pytest.raises(ValueError, match='conflicts with managed parameter'):
        adapter.validate_execution_context('pearlhash', '3.7.2', {'extra_args': '--wallet other'}, coin='PEARL')
    with pytest.raises(ValueError, match='3.6.4'):
        adapter.validate_execution_context('quantus', '3.4.6', {'extra_args': '--future-option'}, coin='QUANTUS')
