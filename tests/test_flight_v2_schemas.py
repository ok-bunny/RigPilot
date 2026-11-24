from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.schemas import FlightMinerCreate, FlightSheetCreate, MinerPackageImport
from server.schemas_v2 import FlightSheetV2Create


def job(
    *,
    job_id: str = "job_one",
    adapter_id: str = "org.xmrig.xmrig",
    miner_version: str = "6.26.0",
    resource: str = "cpu",
    coin: str = "XMR",
    package_id: str = "builtin-xmrig-6.26.0-linux-x64",
    algorithm: str = "randomx",
    devices: list[int] | None = None,
    process_group: str | None = None,
):
    value = {
        "job_id": job_id,
        "adapter_id": adapter_id,
        "adapter_revision": "1.0.0",
        "miner_version": miner_version,
        "resource": resource,
        "coin": coin,
        "wallet_id": "wallet-1",
        "pool_url": "stratum+tcp://pool.example.test:3333",
        "miner_package_id": package_id,
        "algorithm": algorithm,
        "options": {},
        "devices": devices or [],
    }
    if process_group is not None:
        value["process_group"] = process_group
    return value


def sheet(jobs):
    return FlightSheetV2Create.model_validate(
        {"schema_version": 2, "name": "v2", "jobs": jobs}
    )


def test_v2_accepts_one_job_and_generates_distinct_ids_and_groups():
    payload = job()
    payload.pop("job_id")
    first = sheet([payload])
    second = sheet([payload])
    assert first.jobs[0].job_id.startswith("job_")
    assert first.jobs[0].process_group == f"pg_{first.jobs[0].job_id}"
    assert first.jobs[0].job_id != second.jobs[0].job_id


def test_v2_rejects_more_than_four_jobs_and_duplicate_ids():
    with pytest.raises(ValidationError):
        sheet([job(job_id=f"job_{index}") for index in range(5)])
    with pytest.raises(ValidationError, match="job_id values must be unique"):
        sheet([job(), job()])


def test_v2_accepts_two_independent_cpu_jobs_with_separate_groups():
    created = sheet([job(job_id="job_one"), job(job_id="job_two")])
    assert [item.resource for item in created.jobs] == ["cpu", "cpu"]
    assert [item.process_group for item in created.jobs] == ["pg_job_one", "pg_job_two"]


def test_v2_validates_adapter_revision_resource_algorithm_and_pool():
    invalid_revision = job()
    invalid_revision["adapter_revision"] = "1"
    with pytest.raises(ValidationError, match="adapter_revision"):
        sheet([invalid_revision])
    invalid_resource = job()
    invalid_resource["resource"] = "gpu"
    with pytest.raises(ValidationError, match="resource disagrees"):
        sheet([invalid_resource])
    invalid_algorithm = job()
    invalid_algorithm["algorithm"] = "kawpow"
    with pytest.raises(ValidationError, match="algorithm"):
        sheet([invalid_algorithm])
    invalid_pool = job()
    invalid_pool["pool_url"] = "https://pool.example.test:443"
    with pytest.raises(ValidationError, match="pool_url"):
        sheet([invalid_pool])


def test_integrated_pair_requires_same_identity_and_capability():
    primary = job(
        job_id="job_gpu",
        adapter_id="org.doktor83.srbminer-multi",
        miner_version="3.4.6",
        resource="gpu",
        coin="PEARL",
        package_id="builtin-srbminer-3.4.6-linux-x64",
        algorithm="pearlhash",
        devices=[0],
        process_group="pg_integrated",
    )
    secondary = job(
        job_id="job_cpu",
        adapter_id="org.doktor83.srbminer-multi",
        miner_version="3.4.6",
        resource="cpu",
        coin="XMR",
        package_id="builtin-srbminer-3.4.6-linux-x64",
        algorithm="randomx",
        process_group="pg_integrated",
    )
    created = sheet([primary, secondary])
    assert len(created.jobs) == 2
    secondary["miner_version"] = "3.4.5"
    secondary["miner_package_id"] = "builtin-srbminer-3.4.5-linux-x64"
    with pytest.raises(ValidationError, match="integrated jobs must share"):
        sheet([primary, secondary])


def test_independent_gpu_jobs_require_disjoint_explicit_device_sets():
    def gpu(job_id: str, devices: list[int]):
        return job(
            job_id=job_id,
            adapter_id="org.doktor83.srbminer-multi",
            miner_version="3.4.6",
            resource="gpu",
            coin="PEARL",
            package_id="builtin-srbminer-3.4.6-linux-x64",
            algorithm="pearlhash",
            devices=devices,
        )

    assert len(sheet([gpu("job_a", [0]), gpu("job_b", [1])]).jobs) == 2
    with pytest.raises(ValidationError, match="conflicting device ownership"):
        sheet([gpu("job_a", [0]), gpu("job_b", [0])])
    with pytest.raises(ValidationError, match="conflicting device ownership"):
        sheet([gpu("job_a", []), gpu("job_b", [1])])


@pytest.mark.parametrize(
    "adapter_id,miner_type,version,resource,coin,algorithm,reserved",
    [
        ("org.xmrig.xmrig", "xmrig", "6.26.0", "cpu", "XMR", "randomx", "--user"),
        ("org.doktor83.srbminer-multi", "srbminer", "3.4.6", "gpu", "PEARL", "pearlhash", "--wallet"),
        ("org.pearlfortune.pearl-miner", "pearlminer", "2.0.5", "gpu", "PEARL", "pearlhash", "--address"),
    ],
)
def test_extra_args_share_literal_text_and_managed_conflict_rules_in_both_schemas(
    adapter_id, miner_type, version, resource, coin, algorithm, reserved,
):
    extra_args = '--future-boolean --future-value "two words"\r\n--future-number=7'
    expected = extra_args.replace("\r\n", "\n")
    v2 = job(
        adapter_id=adapter_id, miner_version=version, resource=resource,
        coin=coin, algorithm=algorithm,
        package_id=f"builtin-{miner_type}-{version}-linux-x64",
    )
    legacy = {
        "miner_type": miner_type, "miner_version": version, "coin": coin,
        "algorithm": algorithm, "wallet_id": "wallet-1",
        "pool_url": "stratum+tcp://pool.example.test:3333",
    }
    v2["options"] = {"extra_args": extra_args}
    assert sheet([v2]).jobs[0].options["extra_args"] == expected
    assert FlightMinerCreate.model_validate(
        {**legacy, "extra_config": {"extra_args": extra_args}}
    ).extra_config["extra_args"] == expected
    for blank in (None, "", " \t\r\n"):
        v2["options"] = {"extra_args": blank}
        assert sheet([v2]).jobs[0].options["extra_args"] == ""
        assert FlightMinerCreate.model_validate(
            {**legacy, "extra_config": {"extra_args": blank}}
        ).extra_config["extra_args"] == ""
    v2["options"] = {"extra_args": reserved + "=override"}
    with pytest.raises(ValidationError, match="conflicts with managed parameter"):
        sheet([v2])
    with pytest.raises(ValidationError, match="conflicts with managed parameter"):
        FlightMinerCreate.model_validate(
            {**legacy, "extra_config": v2["options"]}
        )


@pytest.mark.parametrize(
    "extra_args",
    [True, ["--future"], "--bad\x00value", '--unclosed "value', "—pearl-k1", "x" * 4097],
)
def test_extra_args_reject_invalid_shape_control_and_quotes(extra_args):
    payload = job()
    payload["options"] = {"extra_args": extra_args}
    with pytest.raises(ValidationError, match="extra_args"):
        sheet([payload])
    with pytest.raises(ValidationError, match="extra_args"):
        FlightMinerCreate.model_validate({
            "miner_type": "xmrig", "coin": "XMR", "wallet_id": "wallet-1",
            "pool_url": "stratum+tcp://pool.example.test:3333",
            "extra_config": {"extra_args": extra_args},
        })


def test_primary_and_secondary_extra_args_keep_separate_values_and_clear():
    request = {
        "name": "independent", "miner_type": "xmrig", "coin": "XMR",
        "wallet_id": "wallet-primary", "pool_url": "stratum+tcp://pool.example.test:3333",
        "extra_config": {"extra_args": '--future-main "primary value"'},
        "secondary_mode": "independent",
        "secondary": {
            "miner_type": "pearlminer", "coin": "PEARL", "wallet_id": "wallet-secondary",
            "pool_url": "pearl://pool.example.test:3334",
            "extra_config": {"extra_args": "--future-secondary"},
        },
    }
    created = FlightSheetCreate.model_validate(request)
    assert created.extra_config["extra_args"] == '--future-main "primary value"'
    assert created.secondary.extra_config["extra_args"] == "--future-secondary"
    request["secondary"]["extra_config"]["extra_args"] = None
    cleared = FlightSheetCreate.model_validate(request)
    assert cleared.extra_config == created.extra_config
    assert cleared.secondary.extra_config["extra_args"] == ""


@pytest.mark.parametrize(
    "miner_type,adapter_id,error",
    [("lolminer", "org.lolliedieb.lolminer", "retired managed miner adapter"),
     ("forgeminer", "org.hashraptor.forgeminer", "retired managed miner adapter"),
     ("synthetic-unsupported-miner", "org.synthetic.unsupported-miner", "unsupported managed miner adapter")],
)
def test_unsupported_miners_are_rejected_as_new_flights_and_package_imports(miner_type, adapter_id, error):
    for identity in (miner_type, adapter_id):
        with pytest.raises(ValidationError, match=error):
            FlightMinerCreate.model_validate({
                "miner_type": identity, "coin": "PEARL", "wallet_id": "wallet-1",
                "pool_url": "stratum+tcp://pool.example.test:3333",
            })
        with pytest.raises(ValidationError, match=error):
            MinerPackageImport.model_validate({
                "name": "retired", "version": "1.0.0", "archive_type": "zip",
                "download_url": "https://packages.example.test/miner.zip",
                "managed_miner_type": identity,
            })
    with pytest.raises(ValidationError, match=error):
        sheet([job(adapter_id=adapter_id)])
