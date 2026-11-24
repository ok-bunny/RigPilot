"""Low-overhead supported-coin workload counter ingestion and bucket queries.

Agents integrate their own verified hashrate into one monotonic absolute
counter per miner epoch. The controller only validates snapshots, subtracts
absolute values and writes five-minute buckets in the heartbeat transaction.
Dashboard queries never scan telemetry.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .miner_adapters.workload import (
    WORKLOAD_BASIS,
    WORKLOAD_COIN_SQL_CHECK,
    canonical_workload_coin,
)

WORKLOAD_BUCKET_SECONDS = 5 * 60
WORKLOAD_MAX_BUCKETS_PER_HEARTBEAT = 72
WORKLOAD_RETENTION_DAYS = 14
_COUNTER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MAX_COUNTER = Decimal("1e100")


class WorkloadTimeRangeError(ValueError):
    """The UTC range cannot be represented as complete workload buckets."""


def _utc_time(value: str | datetime | None, *, assume_naive_utc: bool = False) -> datetime | None:
    if value is None:
        return None
    try:
        # The production Python 3.10 parser needs an explicit UTC offset.
        if isinstance(value, str) and value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            return parsed.replace(tzinfo=timezone.utc) if assume_naive_utc else None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _series_key(coin: str, algorithm: str) -> str:
    encoded = json.dumps(
        [coin, algorithm], separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _counter_total(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = str(value).strip()
    if not text or len(text) > 128:
        return None
    try:
        total = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not total.is_finite() or total < 0 or total > _MAX_COUNTER:
        return None
    return total


def _normalized_counters(values: Any) -> dict[str, dict[str, Any]]:
    """Return one validated absolute counter per supported coin and algorithm."""

    if not isinstance(values, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    invalid: set[str] = set()
    for raw in values[:16]:
        if not isinstance(raw, dict) or set(raw) != {
            "coin",
            "algorithm",
            "counter_epoch",
            "snapshot_seq",
            "estimated_work_total",
            "observed_at",
        }:
            continue
        coin = canonical_workload_coin(raw.get("coin"))
        if coin is None:
            continue
        raw_algorithm = raw.get("algorithm")
        if (not isinstance(raw_algorithm, str)
                or any(ord(character) < 32 or ord(character) == 127 for character in raw_algorithm)):
            continue
        algorithm = raw_algorithm.strip().casefold()
        epoch = str(raw.get("counter_epoch") or "").strip()
        sequence = raw.get("snapshot_seq")
        total = _counter_total(raw.get("estimated_work_total"))
        observed_at = raw.get("observed_at")
        if (
            not algorithm
            or len(algorithm) > 64
            or any(ord(character) < 32 or ord(character) == 127 for character in algorithm)
            or not _COUNTER_ID.fullmatch(epoch)
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or not 0 <= sequence <= 2**63 - 1
            or total is None
            or not isinstance(observed_at, str)
            or _utc_time(observed_at) is None
        ):
            continue
        key = _series_key(coin, algorithm)
        if key in result or key in invalid:
            result.pop(key, None)
            invalid.add(key)
            continue
        result[key] = {
            "series_key": key,
            "coin": coin,
            "algorithm": algorithm,
            "counter_epoch": epoch,
            "snapshot_seq": sequence,
            "estimated_work_total": total,
            "observed_at": observed_at,
        }
    return result


def workload_bucket_start(moment: datetime) -> datetime:
    moment = moment.astimezone(timezone.utc)
    minutes = WORKLOAD_BUCKET_SECONDS // 60
    return moment.replace(minute=moment.minute // minutes * minutes, second=0, microsecond=0)


# Preserve the internal name used by existing workload boundary checks.
_bucket_start = workload_bucket_start

def _split_delta(
    start: datetime,
    end: datetime,
    delta: Decimal,
) -> tuple[list[tuple[datetime, float, float]], bool]:
    """Uniformly split a delta, bounding one heartbeat to 72 bucket writes."""

    elapsed = (end - start).total_seconds()
    numeric_delta = float(delta)
    if (
        elapsed <= 0
        or not math.isfinite(elapsed)
        or not math.isfinite(numeric_delta)
        or numeric_delta < 0
    ):
        return [], False
    buckets: list[tuple[datetime, float, float]] = []
    cursor = start
    while cursor < end and len(buckets) <= WORKLOAD_MAX_BUCKETS_PER_HEARTBEAT:
        bucket = _bucket_start(cursor)
        boundary = bucket + timedelta(seconds=WORKLOAD_BUCKET_SECONDS)
        segment_end = min(end, boundary)
        duration = (segment_end - cursor).total_seconds()
        buckets.append((bucket, numeric_delta * duration / elapsed, duration))
        cursor = segment_end
    if cursor < end or len(buckets) > WORKLOAD_MAX_BUCKETS_PER_HEARTBEAT:
        return [(_bucket_start(end), numeric_delta, 0.0)], True
    return buckets, False


def record_heartbeat_counters(
    db: sqlite3.Connection,
    *,
    agent_id: str,
    received_at: str,
    counters: Any,
) -> None:
    """Consume absolute Agent counters in the accepted heartbeat transaction."""

    moment = _utc_time(received_at)
    current = _normalized_counters(counters)
    if moment is None or not current:
        return
    existing = {
        str(row["series_key"]): row
        for row in db.execute(
            """SELECT series_key,counter_epoch,snapshot_seq,last_total,
                      last_received_at,last_observed_at
               FROM device_work_state WHERE agent_id=?""",
            (agent_id,),
        ).fetchall()
    }
    items = list(current.items())
    writes_used = 0
    for position, (key, snapshot) in enumerate(items):
        previous = existing.get(key)
        should_advance = True
        delta: Decimal | None = None
        previous_received: datetime | None = None
        if previous is not None:
            previous_total = _counter_total(previous["last_total"])
            previous_received = _utc_time(previous["last_received_at"])
            same_epoch = previous["counter_epoch"] == snapshot["counter_epoch"]
            if not same_epoch:
                # Epoch IDs are opaque, not ordered by the device wall clock.
                # Remember retired identities so a delayed old heartbeat cannot
                # replace the new baseline, even after a controller restart.
                if db.execute(
                    """SELECT 1 FROM device_work_retired_epochs
                       WHERE agent_id=? AND series_key=? AND counter_epoch=?""",
                    (agent_id, key, snapshot["counter_epoch"]),
                ).fetchone():
                    continue
                db.execute(
                    """INSERT OR IGNORE INTO device_work_retired_epochs
                       (agent_id,series_key,counter_epoch) VALUES(?,?,?)""",
                    (agent_id, key, previous["counter_epoch"]),
                )
            sequence_advanced = int(snapshot["snapshot_seq"]) > int(
                previous["snapshot_seq"]
            )
            total_not_rewound = (
                previous_total is not None
                and snapshot["estimated_work_total"] >= previous_total
            )
            if same_epoch and sequence_advanced and total_not_rewound:
                delta = snapshot["estimated_work_total"] - previous_total
            elif same_epoch:
                # Cached duplicate snapshots are expected because collection
                # and heartbeat cadences differ. Conflicting/receding values
                # cannot replace the last good baseline or create work.
                should_advance = False
        if delta is not None and previous_received is not None and delta > 0:
            segments, recovered = _split_delta(previous_received, moment, delta)
            # The cap applies to the entire authenticated heartbeat, not each
            # algorithm independently. Reserve one recovery row for every
            # remaining series; a wide/long interval is collapsed rather than
            # causing a write spike on a one-core controller.
            remaining_snapshots = len(items) - position - 1
            write_limit = max(
                1,
                WORKLOAD_MAX_BUCKETS_PER_HEARTBEAT
                - writes_used
                - remaining_snapshots,
            )
            if len(segments) > write_limit:
                segments = [(_bucket_start(moment), float(delta), 0.0)]
                recovered = True
            writes_used += len(segments)
            for bucket, work, coverage in segments:
                db.execute(
                    """INSERT INTO device_work_buckets
                       (agent_id,series_key,bucket_start,coin,algorithm,
                        estimated_hashes,recovered_hashes,coverage_seconds,
                        interval_count,recovery_count)
                       VALUES(?,?,?,?,?,?,?,?,1,?)
                       ON CONFLICT(agent_id,series_key,bucket_start)
                       DO UPDATE SET
                         estimated_hashes=estimated_hashes+excluded.estimated_hashes,
                         recovered_hashes=recovered_hashes+excluded.recovered_hashes,
                         coverage_seconds=coverage_seconds+excluded.coverage_seconds,
                         interval_count=interval_count+1,
                         recovery_count=recovery_count+excluded.recovery_count""",
                    (
                        agent_id,
                        key,
                        bucket.isoformat(timespec="seconds"),
                        snapshot["coin"],
                        snapshot["algorithm"],
                        work,
                        work if recovered else 0.0,
                        coverage,
                        1 if recovered else 0,
                    ),
                )
        if not should_advance:
            continue
        db.execute(
            """INSERT INTO device_work_state
               (agent_id,series_key,counter_epoch,snapshot_seq,last_total,
                coin,algorithm,last_received_at,last_observed_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(agent_id,series_key) DO UPDATE SET
                 counter_epoch=excluded.counter_epoch,
                 snapshot_seq=excluded.snapshot_seq,
                 last_total=excluded.last_total,
                 coin=excluded.coin,
                 algorithm=excluded.algorithm,
                 last_received_at=excluded.last_received_at,
                 last_observed_at=excluded.last_observed_at""",
            (
                agent_id,
                key,
                snapshot["counter_epoch"],
                snapshot["snapshot_seq"],
                str(snapshot["estimated_work_total"]),
                snapshot["coin"],
                snapshot["algorithm"],
                moment.isoformat(timespec="microseconds"),
                snapshot["observed_at"],
            ),
        )


def acknowledged_heartbeat_counters(
    db: sqlite3.Connection,
    *,
    agent_id: str,
    counters: Any,
) -> list[dict[str, Any]]:
    """Read submitted snapshots matching final state in the accepted transaction.

    The caller may publish these acknowledgements only after commit succeeds.
    Normalization deliberately shares ingestion's batch and duplicate rules.
    """

    acknowledgements: list[dict[str, Any]] = []
    for key, snapshot in _normalized_counters(counters).items():
        stored = db.execute(
            """SELECT s.coin,s.algorithm,s.counter_epoch,s.snapshot_seq,
                      s.last_total,s.last_observed_at
               FROM device_work_state AS s
               WHERE s.agent_id=? AND s.series_key=?
                 AND NOT EXISTS (
                     SELECT 1 FROM device_work_retired_epochs AS r
                     WHERE r.agent_id=s.agent_id AND r.series_key=s.series_key
                       AND r.counter_epoch=s.counter_epoch
                 )""",
            (agent_id, key),
        ).fetchone()
        if stored is None or (
            stored["coin"] != snapshot["coin"]
            or stored["algorithm"] != snapshot["algorithm"]
            or stored["counter_epoch"] != snapshot["counter_epoch"]
            or stored["snapshot_seq"] != snapshot["snapshot_seq"]
            or _counter_total(stored["last_total"]) != snapshot["estimated_work_total"]
            or _utc_time(stored["last_observed_at"]) != _utc_time(snapshot["observed_at"])
        ):
            continue
        acknowledgements.append(
            {
                "coin": stored["coin"],
                "algorithm": stored["algorithm"],
                "counter_epoch": stored["counter_epoch"],
                "snapshot_seq": int(stored["snapshot_seq"]),
                "estimated_work_total": str(stored["last_total"]),
                "observed_at": str(stored["last_observed_at"]),
            }
        )
    return acknowledgements


def _format_work(value: float) -> str:
    units = ("H", "kH", "MH", "GH", "TH", "PH", "EH", "ZH", "YH")
    amount = max(0.0, float(value))
    index = 0
    while amount >= 1000 and index < len(units) - 1:
        amount /= 1000
        index += 1
    digits = 0 if amount >= 100 else 1 if amount >= 10 else 2
    return f"{amount:.{digits}f} {units[index]}"


def _merge_devices_with_same_name(
    devices: list[dict[str, Any]], *, include_points: bool
) -> list[dict[str, Any]]:
    """Merge display rows that have the same user-defined device name.

    Counters and buckets remain isolated by immutable Agent ID.  This helper
    only projects the fleet-wide dashboard response by the visible custom
    name, so renaming devices never rewrites or mixes stored history.
    """

    merged: dict[str, dict[str, Any]] = {}
    for source in devices:
        name = str(source.get("agent_name") or "").strip()
        # Agent names are already stripped and non-empty at the API boundary.
        # Keep matching exact so deliberately distinct casing remains distinct.
        key = name
        target = merged.get(key)
        if target is None:
            target = dict(source)
            target["agent_ids"] = [str(source["agent_id"])]
            hostname = str(source.get("hostname") or "").strip()
            target["hostnames"] = [hostname] if hostname else []
            if include_points:
                target["points"] = list(source.get("points") or [])
            merged[key] = target
            continue

        target["value"] += float(source["value"])
        target["recovered_value"] += float(source["recovered_value"])
        target["coverage_seconds"] += float(source["coverage_seconds"])
        target["has_coverage_gap"] = bool(
            target["has_coverage_gap"] or source["has_coverage_gap"]
        )
        source_agent_id = str(source["agent_id"])
        if source_agent_id not in target["agent_ids"]:
            target["agent_ids"].append(source_agent_id)
        source_hostname = str(source.get("hostname") or "").strip()
        if source_hostname and source_hostname not in target["hostnames"]:
            target["hostnames"].append(source_hostname)
        if include_points:
            target["points"].extend(source.get("points") or [])

    result: list[dict[str, Any]] = []
    for device in merged.values():
        device["agent_ids"].sort()
        device["hostnames"].sort(key=str.casefold)
        device["device_count"] = len(device["agent_ids"])
        # A single hostname would misleadingly identify a multi-Agent group.
        device["hostname"] = (
            device["hostnames"][0] if len(device["hostnames"]) == 1 else ""
        )
        if include_points:
            points: dict[str, dict[str, Any]] = {}
            for source_point in device.get("points") or []:
                timestamp = str(source_point["timestamp"])
                point = points.get(timestamp)
                if point is None:
                    point = dict(source_point)
                    points[timestamp] = point
                    continue
                point["value"] += float(source_point["value"])
                point["value_number"] = float(point["value"])
                point["recovered_value"] += float(source_point["recovered_value"])
                point["coverage_seconds"] += float(source_point["coverage_seconds"])
                point["has_coverage_gap"] = bool(
                    point["has_coverage_gap"] or source_point["has_coverage_gap"]
                )
            device["points"] = []
            for timestamp in sorted(points):
                point = points[timestamp]
                point["value_number"] = float(point["value"])
                point["value_text"] = _format_work(float(point["value"]))
                device["points"].append(point)
        result.append(device)
    return result


def _format_device_value(device: dict[str, Any], total: float) -> None:
    value = float(device["value"])
    device["value_number"] = value
    device["value_text"] = _format_work(value)
    device["percentage"] = (value / total * 100.0) if total > 0 else 0.0


def _device_sort_key(device: dict[str, Any]) -> tuple[float, str, str]:
    return (-float(device["value"]), str(device["agent_name"]).casefold(), str(device["agent_id"]))


def workload_summary(
    db: sqlite3.Connection,
    *,
    start: datetime,
    end: datetime,
    agent_id: str | None,
    algorithm: str | None,
    include_points: bool,
    coin: str | None = None,
    coins: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Project supported raw buckets by coin without changing their identities."""

    try:
        effective_start = _bucket_start(start)
        effective_end = _bucket_start(end)
        if effective_end < end:
            effective_end += timedelta(seconds=WORKLOAD_BUCKET_SECONDS)
    except OverflowError as exc:
        raise WorkloadTimeRangeError("Workload bucket range cannot be represented") from exc
    clauses = ["b.bucket_start>=?", "b.bucket_start<?", WORKLOAD_COIN_SQL_CHECK.format(column="b.coin")]
    parameters: list[Any] = [
        effective_start.isoformat(timespec="seconds"),
        effective_end.isoformat(timespec="seconds"),
    ]

    if coin is not None:
        selected_coin = canonical_workload_coin(coin)
        if selected_coin is None:
            clauses.append("0=1")
        else:
            clauses.append("b.coin=?")
            parameters.append(selected_coin)
    if coins is not None:
        normalized = [] if isinstance(coins, (str, bytes)) else [canonical_workload_coin(value) for value in coins]
        if not normalized or any(value is None for value in normalized):
            clauses.append("0=1")
        else:
            selected = sorted(set(normalized))
            clauses.append("b.coin IN (" + ",".join("?" for _ in selected) + ")")
            parameters.extend(selected)
    if algorithm is not None:
        clauses.append("b.algorithm=?")
        parameters.append(algorithm.strip().casefold())
    where = " AND ".join(clauses)
    if include_points:
        rows = db.execute(
            f"""SELECT b.agent_id,a.name AS agent_name,a.hostname,
                       b.series_key,b.coin,b.algorithm,b.bucket_start,
                       b.estimated_hashes,b.recovered_hashes,
                       b.coverage_seconds,b.interval_count,b.recovery_count
                FROM device_work_buckets AS b
                JOIN agents AS a ON a.id=b.agent_id
                WHERE {where}
                ORDER BY b.coin,b.agent_id,b.bucket_start,b.algorithm,b.series_key""",
            parameters,
        ).fetchall()
    else:
        rows = db.execute(
            f"""SELECT b.agent_id,a.name AS agent_name,a.hostname,
                       b.series_key,b.coin,b.algorithm,NULL AS bucket_start,
                       SUM(b.estimated_hashes) AS estimated_hashes,
                       SUM(b.recovered_hashes) AS recovered_hashes,
                       SUM(b.coverage_seconds) AS coverage_seconds,
                       SUM(b.interval_count) AS interval_count,
                       SUM(b.recovery_count) AS recovery_count
                FROM device_work_buckets AS b
                JOIN agents AS a ON a.id=b.agent_id
                WHERE {where}
                GROUP BY b.agent_id,b.series_key,b.coin,b.algorithm,a.name,a.hostname
                ORDER BY b.coin,b.algorithm,b.series_key,
                         a.name COLLATE NOCASE,b.agent_id""",
            parameters,
        ).fetchall()

    grouped: dict[str, dict[str, Any]] = {}
    devices: dict[tuple[str, str], dict[str, Any]] = {}
    points: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = "coin:" + str(row["coin"])
        series = grouped.setdefault(
            key,
            {
                "series_key": key,
                "coin": str(row["coin"]),
                "algorithm": None,
                "algorithms": [],
                "source_series_keys": [],
                "unit": "H",
                "basis": WORKLOAD_BASIS,
                "estimated": True,
                "total": 0.0,
                "recovered_total": 0.0,
                "coverage_seconds": 0.0,
                "has_coverage_gap": False,
                "devices": [],
            },
        )
        if str(row["algorithm"]) not in series["algorithms"]:
            series["algorithms"].append(str(row["algorithm"]))
        if str(row["series_key"]) not in series["source_series_keys"]:
            series["source_series_keys"].append(str(row["series_key"]))
        device_key = (key, str(row["agent_id"]))
        device = devices.get(device_key)
        if device is None:
            device = {
                "agent_id": str(row["agent_id"]),
                "agent_name": str(row["agent_name"]),
                "hostname": str(row["hostname"]),
                "value": 0.0,
                "recovered_value": 0.0,
                "coverage_seconds": 0.0,
                "has_coverage_gap": False,
            }
            if include_points:
                device["points"] = []
            devices[device_key] = device
            series["devices"].append(device)
        work = float(row["estimated_hashes"])
        recovered = float(row["recovered_hashes"])
        coverage = float(row["coverage_seconds"])
        has_gap = int(row["recovery_count"]) > 0
        series["total"] += work
        series["recovered_total"] += recovered
        series["coverage_seconds"] += coverage
        series["has_coverage_gap"] = series["has_coverage_gap"] or has_gap
        device["value"] += work
        device["recovered_value"] += recovered
        device["coverage_seconds"] += coverage
        device["has_coverage_gap"] = device["has_coverage_gap"] or has_gap
        if include_points:
            point_key = (*device_key, str(row["bucket_start"]))
            point = points.get(point_key)
            if point is None:
                point = {"timestamp": point_key[2], "value": 0.0, "recovered_value": 0.0,
                         "coverage_seconds": 0.0, "has_coverage_gap": False}
                points[point_key] = point
                device["points"].append(point)
            point["value"] += work
            point["value_number"] = point["value"]
            point["value_text"] = _format_work(point["value"])
            point["recovered_value"] += recovered
            point["coverage_seconds"] += coverage
            point["has_coverage_gap"] = point["has_coverage_gap"] or has_gap

    # Keep every selected coin's fleet denominator even when the requested
    # Agent has no contribution in that coin and its visible series is omitted.
    fleet_totals_by_coin = {
        series["coin"]: float(series["total"])
        for series in grouped.values()
    }
    visible_series: list[dict[str, Any]] = []
    for series in grouped.values():
        series["algorithms"].sort()
        series["source_series_keys"].sort()
        total = float(series["total"])
        individual_devices = []
        for source_device in series["devices"]:
            device = dict(source_device)
            if include_points:
                device["points"] = [dict(point) for point in source_device["points"]]
            _format_device_value(device, total)
            individual_devices.append(device)
        if agent_id is not None:
            individual_devices = [device for device in individual_devices if device["agent_id"] == agent_id]
        series["individual_devices"] = sorted(individual_devices, key=_device_sort_key)
        if agent_id is None:
            series["devices"] = _merge_devices_with_same_name(
                series["devices"], include_points=include_points
            )
        series["series_total"] = total
        series["total_number"] = total
        series["total_text"] = _format_work(total)
        for device in series["devices"]:
            _format_device_value(device, total)
        if agent_id is not None:
            series["devices"] = [
                device for device in series["devices"]
                if device["agent_id"] == agent_id
            ]
            if not series["devices"]:
                continue
        series["devices"].sort(key=_device_sort_key)
        visible_series.append(series)
    return {
        "from": start.isoformat(timespec="seconds"),
        "to": end.isoformat(timespec="seconds"),
        "effective_from": effective_start.isoformat(timespec="seconds"),
        "effective_to": effective_end.isoformat(timespec="seconds"),
        "bucket_minutes": WORKLOAD_BUCKET_SECONDS // 60,
        "max_range_days": WORKLOAD_RETENTION_DAYS,
        "basis": WORKLOAD_BASIS,
        "estimated": True,
        "group_by": "coin",
        "available_coins": sorted({item["coin"] for item in grouped.values()}),
        "fleet_totals_by_coin": fleet_totals_by_coin,
        "series": sorted(
            visible_series,
            key=lambda item: str(item["coin"]),
        ),
    }
