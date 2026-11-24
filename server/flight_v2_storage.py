"""Normalized persistence and fail-closed legacy projection for Flight V2.

The legacy ``flight_sheets`` columns remain the rollback surface.  This module
adds a queryable job table beside them and projects only built-in managed
miners whose complete adapter runtime is available.  A custom/generic or
ambiguous row stays schema version 1 and is never partially projected.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sqlite3
import uuid
from typing import Any, Mapping

from .miner_adapters import MINER_ADAPTERS
from .miner_adapters.runtime_v1 import (
    AdapterRuntimeDeclarationError,
    runtime_sha256_for,
)
from .schemas_v2 import adapter_public_version


_KNOWN_MINERS = frozenset({"xmrig", "srbminer", "pearlminer"})
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


FLIGHT_V2_JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS flight_sheet_jobs (
    flight_sheet_id TEXT NOT NULL
        REFERENCES flight_sheets(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 3),
    job_id TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    adapter_revision TEXT NOT NULL,
    adapter_runtime_sha256 TEXT NOT NULL
        CHECK(length(adapter_runtime_sha256)=64),
    miner_version TEXT NOT NULL,
    resource TEXT NOT NULL CHECK(resource IN ('cpu','gpu')),
    coin TEXT NOT NULL,
    wallet_id TEXT NOT NULL REFERENCES wallets(id) ON DELETE RESTRICT,
    pool_url TEXT NOT NULL,
    miner_package_id TEXT REFERENCES miner_packages(id) ON DELETE RESTRICT,
    execution_mode TEXT NOT NULL CHECK(execution_mode IN ('native','wsl')),
    algorithm TEXT NOT NULL,
    pool_password TEXT NOT NULL DEFAULT 'x',
    device_indices_json TEXT NOT NULL DEFAULT '[]',
    options_json TEXT NOT NULL DEFAULT '{}',
    process_group TEXT NOT NULL,
    PRIMARY KEY(flight_sheet_id, position),
    UNIQUE(flight_sheet_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_flight_sheet_jobs_adapter
    ON flight_sheet_jobs(adapter_id, adapter_revision);
CREATE INDEX IF NOT EXISTS idx_flight_sheet_jobs_wallet
    ON flight_sheet_jobs(wallet_id);
CREATE INDEX IF NOT EXISTS idx_flight_sheet_jobs_package
    ON flight_sheet_jobs(miner_package_id);
"""


@dataclass(frozen=True, slots=True)
class _ProjectedJob:
    position: int
    job_id: str
    adapter_id: str
    adapter_revision: str
    adapter_runtime_sha256: str
    miner_version: str
    resource: str
    coin: str
    wallet_id: str
    pool_url: str
    miner_package_id: str | None
    execution_mode: str
    algorithm: str
    pool_password: str
    device_indices_json: str
    options_json: str
    process_group: str

    def sql_values(self, flight_sheet_id: str) -> tuple[object, ...]:
        return (
            flight_sheet_id,
            self.position,
            self.job_id,
            self.adapter_id,
            self.adapter_revision,
            self.adapter_runtime_sha256,
            self.miner_version,
            self.resource,
            self.coin,
            self.wallet_id,
            self.pool_url,
            self.miner_package_id,
            self.execution_mode,
            self.algorithm,
            self.pool_password,
            self.device_indices_json,
            self.options_json,
            self.process_group,
        )


def canonical_legacy_job_id(flight_sheet_id: str, position: int) -> str:
    """Stable per-sheet job identity independent of editable configuration."""

    if position not in range(4):
        raise ValueError("Flight V2 position must be between 0 and 3")
    value = uuid.uuid5(
        uuid.NAMESPACE_URL, f"rigpilot:flight-v2:{flight_sheet_id}:{position}"
    ).hex
    return f"job_{value}"


def _canonical_json_object(value: object) -> str:
    if value in (None, ""):
        parsed: object = {}
    elif isinstance(value, str):
        parsed = json.loads(value)
    else:
        parsed = value
    if not isinstance(parsed, dict):
        raise ValueError("legacy options must be a JSON object")
    return json.dumps(
        parsed, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _canonical_device_indices(value: object) -> str:
    if value in (None, ""):
        parsed: object = []
    elif isinstance(value, str):
        parsed = json.loads(value)
    else:
        parsed = value
    if (
        not isinstance(parsed, list)
        or any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < 0
            or item > 31
            for item in parsed
        )
        or len(parsed) != len(set(parsed))
    ):
        raise ValueError("legacy GPU indices are invalid")
    return json.dumps(parsed, separators=(",", ":"))


def _field(row: Mapping[str, Any], role: str, name: str) -> Any:
    return row.get(name if role == "primary" else f"secondary_{name}")


def _project_leg(
    row: Mapping[str, Any],
    *,
    role: str,
    position: int,
    process_group: str,
) -> _ProjectedJob:
    miner_type = str(_field(row, role, "miner_type") or "").strip().lower()
    if miner_type not in _KNOWN_MINERS:
        raise ValueError("legacy miner is not a built-in managed adapter")
    adapter = MINER_ADAPTERS.require(miner_type)
    version = adapter.release.normalize_version(_field(row, role, "miner_version"))
    coin = str(_field(row, role, "coin") or "").strip().upper()
    if not coin:
        raise ValueError("legacy coin is missing")
    algorithm = adapter.normalize_algorithm(
        _field(row, role, "algorithm"), coin=coin
    )
    resource = adapter.algorithm_resource(algorithm, coin=coin)
    wallet_id = str(_field(row, role, "wallet_id") or "").strip()
    pool_url = str(_field(row, role, "pool_url") or "").strip()
    if not wallet_id or not pool_url:
        raise ValueError("legacy wallet or pool is missing")
    package_value = _field(row, role, "miner_package_id")
    package_id = str(package_value).strip() if package_value is not None else None
    if not package_id:
        package_id = None
    execution_mode = str(
        _field(row, role, "execution_mode") or "native"
    ).strip().lower()
    if execution_mode not in {"native", "wsl"}:
        raise ValueError("legacy execution mode is invalid")
    if execution_mode == "wsl" and resource != "gpu":
        raise ValueError("legacy WSL CPU jobs cannot be projected")
    runtime_sha256 = runtime_sha256_for(adapter)
    if _HEX_SHA256.fullmatch(runtime_sha256) is None:
        raise ValueError("adapter runtime identity is invalid")
    options = _field(row, role, "extra_config_json")
    devices = _field(row, role, "device_indices_json")
    password = _field(row, role, "pool_password")
    return _ProjectedJob(
        position=position,
        job_id=canonical_legacy_job_id(str(row["id"]), position),
        adapter_id=adapter.adapter_id,
        adapter_revision=adapter_public_version(adapter),
        adapter_runtime_sha256=runtime_sha256,
        miner_version=version,
        resource=resource,
        coin=coin,
        wallet_id=wallet_id,
        pool_url=pool_url,
        miner_package_id=package_id,
        execution_mode=execution_mode,
        algorithm=algorithm,
        pool_password=str(password if password is not None else "x"),
        device_indices_json=_canonical_device_indices(devices),
        options_json=_canonical_json_object(options),
        process_group=process_group,
    )


def project_legacy_jobs(row: Mapping[str, Any]) -> tuple[_ProjectedJob, ...] | None:
    """Return a complete deterministic projection or ``None`` fail-closed."""

    sheet_id = str(row.get("id") or "").strip()
    if not sheet_id:
        return None
    primary_type = str(row.get("miner_type") or "").strip().lower()
    if primary_type not in _KNOWN_MINERS:
        return None
    has_secondary = bool(
        str(row.get("secondary_miner_type") or "").strip()
        or str(row.get("secondary_coin") or "").strip()
    )
    mode = str(row.get("secondary_mode") or "integrated").strip().lower()
    if mode not in {"integrated", "independent"}:
        return None
    try:
        primary_group = "pg_integrated" if has_secondary and mode == "integrated" else "pg_primary"
        primary = _project_leg(
            row, role="primary", position=0, process_group=primary_group
        )
        if not has_secondary:
            return (primary,)
        secondary_group = primary_group if mode == "integrated" else "pg_secondary"
        secondary = _project_leg(
            row, role="secondary", position=1, process_group=secondary_group
        )
        if mode == "integrated":
            if (
                primary.adapter_id,
                primary.adapter_revision,
                primary.adapter_runtime_sha256,
                primary.miner_version,
                primary.miner_package_id,
                primary.execution_mode,
            ) != (
                secondary.adapter_id,
                secondary.adapter_revision,
                secondary.adapter_runtime_sha256,
                secondary.miner_version,
                secondary.miner_package_id,
                secondary.execution_mode,
            ):
                return None
            adapter = MINER_ADAPTERS.require(primary.adapter_id)
            if "integrated_dual" not in adapter.capabilities:
                return None
        elif primary.resource == secondary.resource == "gpu":
            first = set(json.loads(primary.device_indices_json))
            second = set(json.loads(secondary.device_indices_json))
            if not first or not second or first.intersection(second):
                return None
        return primary, secondary
    except (
        AdapterRuntimeDeclarationError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None


def ensure_flight_v2_storage(db: sqlite3.Connection) -> None:
    """Create the normalized schema and project safe legacy rows idempotently."""

    db.row_factory = sqlite3.Row
    columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(flight_sheets)")}
    if "schema_version" not in columns:
        db.execute(
            "ALTER TABLE flight_sheets ADD COLUMN schema_version "
            "INTEGER NOT NULL DEFAULT 1 CHECK(schema_version IN (1,2))"
        )
    db.executescript(FLIGHT_V2_JOBS_SCHEMA)
    legacy_rows = db.execute(
        "SELECT * FROM flight_sheets WHERE schema_version=1 ORDER BY id"
    ).fetchall()
    insert_sql = """
        INSERT OR IGNORE INTO flight_sheet_jobs (
            flight_sheet_id,position,job_id,adapter_id,adapter_revision,
            adapter_runtime_sha256,miner_version,resource,coin,wallet_id,
            pool_url,miner_package_id,execution_mode,algorithm,pool_password,
            device_indices_json,options_json,process_group
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    for sqlite_row in legacy_rows:
        row = dict(sqlite_row)
        jobs = project_legacy_jobs(row)
        if not jobs:
            continue
        # A previous interrupted development migration must not be completed
        # from a different projection.  Only an empty or exact row set is safe.
        existing = db.execute(
            """SELECT position,job_id,adapter_id,adapter_revision,
                      adapter_runtime_sha256,miner_version,resource,coin,
                      wallet_id,pool_url,miner_package_id,execution_mode,
                      algorithm,pool_password,device_indices_json,options_json,
                      process_group
               FROM flight_sheet_jobs WHERE flight_sheet_id=? ORDER BY position""",
            (row["id"],),
        ).fetchall()
        desired = [job.sql_values(str(row["id"]))[1:] for job in jobs]
        actual = [tuple(item) for item in existing]
        if actual and actual != desired:
            continue
        for job in jobs:
            db.execute(insert_sql, job.sql_values(str(row["id"])))
        count = int(
            db.execute(
                "SELECT COUNT(*) FROM flight_sheet_jobs WHERE flight_sheet_id=?",
                (row["id"],),
            ).fetchone()[0]
        )
        if count == len(jobs):
            db.execute(
                "UPDATE flight_sheets SET schema_version=2 WHERE id=? AND schema_version=1",
                (row["id"],),
            )


__all__ = [
    "FLIGHT_V2_JOBS_SCHEMA",
    "canonical_legacy_job_id",
    "ensure_flight_v2_storage",
    "project_legacy_jobs",
]
