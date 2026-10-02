#!/usr/bin/env python3
"""
Live regression / burst test for the Seat Reservation service.

Usage:
    python burst.py <base_url> [total_requests] [concurrency]

Examples:
    python burst.py https://seat-reservation-production-ee5f.up.railway.app
    python burst.py https://seat-reservation-production-ee5f.up.railway.app 20000 200

Dependencies:
    pip install aiohttp

What this script validates:
    1. /health/live
    2. /health/ready
    3. /actuator/prometheus
    4. 500-request synchronized hot-seat race
    5. 10-request synchronized per-user limit race
    6. 50-request synchronized same-key idempotency race
    7. same-key/different-body -> 409
    8. multi-seat all-or-nothing behavior
    9. configurable full burst (default 20,000 requests / 200 concurrency)
   10. final show reconciliation
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

import aiohttp


DEFAULT_BASE_URL = "https://seat-reservation-production-ee5f.up.railway.app"
DEFAULT_TOTAL_REQUESTS = 20_000
DEFAULT_CONCURRENCY = 200
REQUEST_TIMEOUT_SECONDS = 60


@dataclass
class Result:
    status: int
    body: Any
    duration: float
    user_id: str
    idempotency_key: str
    seats: list[str]
    request_index: int = -1

    @property
    def reason(self) -> str:
        if isinstance(self.body, dict):
            for key in ("code", "reason", "error_code"):
                value = self.body.get(key)
                if value:
                    return str(value)
        return "UNKNOWN"


async def http_request(
    session: aiohttp.ClientSession,
    method: str,
    base_url: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    user_id: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, Any, float]:
    headers = {"Content-Type": "application/json"}

    if user_id is not None:
        headers["Authorization"] = f"Bearer {user_id}"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key

    url = f"{base_url}{path}"
    started = time.perf_counter()

    try:
        async with session.request(
            method,
            url,
            json=body,
            headers=headers,
        ) as response:
            text = await response.text()
            duration = time.perf_counter() - started

            try:
                parsed = json.loads(text) if text else {}
            except json.JSONDecodeError:
                parsed = text

            return response.status, parsed, duration

    except Exception as exc:  # client/network failure, not HTTP 5xx
        duration = time.perf_counter() - started
        return 0, {"client_error": str(exc)}, duration


async def create_show(
    session: aiohttp.ClientSession,
    base_url: str,
    *,
    name: str,
    seats: list[str],
    per_user_limit: int,
) -> str:
    status, body, _ = await http_request(
        session,
        "POST",
        base_url,
        "/shows",
        body={
            "name": name,
            "seats": seats,
            "price_paise": 10_000,
            "per_user_limit": per_user_limit,
        },
        user_id="admin",
    )

    if status not in (200, 201):
        raise RuntimeError(f"Create show failed: HTTP {status}: {body}")

    if not isinstance(body, dict) or not body.get("id"):
        raise RuntimeError(f"Create show response has no id: {body}")

    return str(body["id"])


async def get_show(
    session: aiohttp.ClientSession,
    base_url: str,
    show_id: str,
) -> tuple[int, Any]:
    status, body, _ = await http_request(
        session,
        "GET",
        base_url,
        f"/shows/{show_id}",
    )
    return status, body


async def check_health(session: aiohttp.ClientSession, base_url: str) -> bool:
    print("\n=== HEALTH & METRICS ===")

    passed = True
    for name, path in (
        ("Liveness", "/health/live"),
        ("Readiness", "/health/ready"),
        ("Prometheus", "/actuator/prometheus"),
    ):
        status, body, _ = await http_request(
            session,
            "GET",
            base_url,
            path,
        )
        ok = status == 200
        passed &= ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: HTTP {status}")
        if not ok:
            print(f"       {body}")

    return passed


async def run_synchronized_batch(
    session: aiohttp.ClientSession,
    base_url: str,
    show_id: str,
    tasks: list[tuple[str, str, list[str]]],
) -> list[Result]:
    """Start all tasks in this batch as close together as asyncio permits."""
    start_event = asyncio.Event()

    async def one(
        index: int,
        user_id: str,
        key: str,
        seats: list[str],
    ) -> Result:
        await start_event.wait()
        status, body, duration = await http_request(
            session,
            "POST",
            base_url,
            f"/shows/{show_id}/reserve",
            body={"seats": seats},
            user_id=user_id,
            idempotency_key=key,
        )
        return Result(
            status=status,
            body=body,
            duration=duration,
            user_id=user_id,
            idempotency_key=key,
            seats=seats,
            request_index=index,
        )

    coroutines = [
        one(i, user_id, key, seats)
        for i, (user_id, key, seats) in enumerate(tasks)
    ]
    pending = [asyncio.create_task(c) for c in coroutines]

    # Let every request reach the event wait before opening the gate.
    await asyncio.sleep(0)
    start_event.set()

    return await asyncio.gather(*pending)


async def run_in_batches(
    session: aiohttp.ClientSession,
    base_url: str,
    show_id: str,
    tasks: list[tuple[str, str, list[str]]],
    concurrency: int,
) -> list[Result]:
    results: list[Result] = []

    for start in range(0, len(tasks), concurrency):
        batch = tasks[start : start + concurrency]
        batch_results = await run_synchronized_batch(
            session,
            base_url,
            show_id,
            batch,
        )
        results.extend(batch_results)

    return results


def status_counter(results: list[Result]) -> Counter:
    return Counter(result.status for result in results)


def server_5xx(results: list[Result]) -> int:
    return sum(1 for result in results if 500 <= result.status <= 599)


def client_errors(results: list[Result]) -> int:
    return sum(1 for result in results if result.status == 0)


def print_distribution(results: list[Result]) -> None:
    counts = status_counter(results)
    reasons = Counter(
        result.reason
        for result in results
        if result.status == 409
    )

    print(f"201:                 {counts.get(201, 0)}")
    print(f"200:                 {counts.get(200, 0)}")
    print(f"409:                 {counts.get(409, 0)}")
    print(f"other 4xx:           {sum(v for k, v in counts.items() if 400 <= k < 500 and k != 409)}")
    print(f"5xx:                 {server_5xx(results)}")
    print(f"client/network:      {client_errors(results)}")

    if reasons:
        print("409 reasons:")
        for reason, count in sorted(reasons.items()):
            print(f"  {reason:<28} {count}")


def invariant_passes(show: dict[str, Any]) -> bool:
    total = int(show.get("total_seats", -1))
    available = int(show.get("available", -1))
    confirmed = int(show.get("confirmed", -1))
    held = int(show.get("held", 0))
    return available + held + confirmed == total


async def test_hot_seat(
    session: aiohttp.ClientSession,
    base_url: str,
) -> bool:
    print("\n=== HOT-SEAT CONCURRENCY ===")

    seats = [f"A{i}" for i in range(1, 21)]
    show_id = await create_show(
        session,
        base_url,
        name=f"hot-seat-{uuid.uuid4()}",
        seats=seats,
        per_user_limit=4,
    )

    tasks = [
        (
            f"hot-user-{i}",
            f"hot-key-{uuid.uuid4()}",
            ["A1"],
        )
        for i in range(500)
    ]

    results = await run_in_batches(
        session,
        base_url,
        show_id,
        tasks,
        concurrency=500,
    )

    print_distribution(results)

    status_ok = (
        sum(r.status == 201 for r in results) == 1
        and sum(r.status == 409 for r in results) == 499
        and server_5xx(results) == 0
        and client_errors(results) == 0
        and all(r.status in (201, 409) for r in results)
    )

    status, show = await get_show(session, base_url, show_id)
    state_ok = (
        status == 200
        and isinstance(show, dict)
        and show.get("total_seats") == 20
        and show.get("confirmed") == 1
        and show.get("available") == 19
        and invariant_passes(show)
    )

    print(
        f"Final state: total={show.get('total_seats') if isinstance(show, dict) else '?'} "
        f"available={show.get('available') if isinstance(show, dict) else '?'} "
        f"confirmed={show.get('confirmed') if isinstance(show, dict) else '?'}"
    )

    passed = status_ok and state_ok
    print(f"[{'PASS' if passed else 'FAIL'}] Hot-seat correctness")
    return passed


async def test_user_limit(
    session: aiohttp.ClientSession,
    base_url: str,
) -> bool:
    print("\n=== PER-USER LIMIT CONCURRENCY ===")

    seats = [f"B{i}" for i in range(1, 21)]
    show_id = await create_show(
        session,
        base_url,
        name=f"user-limit-{uuid.uuid4()}",
        seats=seats,
        per_user_limit=4,
    )

    tasks = [
        (
            "limit-user",
            f"limit-key-{i}-{uuid.uuid4()}",
            [f"B{i + 1}"],
        )
        for i in range(10)
    ]

    results = await run_synchronized_batch(
        session,
        base_url,
        show_id,
        tasks,
    )

    print_distribution(results)

    confirmed = sum(r.status == 201 for r in results)
    limit_declines = sum(
        r.status == 409 and r.reason in {
            "PER_USER_LIMIT",
            "per-user-limit",
        }
        for r in results
    )

    status_ok = (
        confirmed == 4
        and limit_declines == 6
        and server_5xx(results) == 0
        and client_errors(results) == 0
        and all(r.status in (201, 409) for r in results)
    )

    status, show = await get_show(session, base_url, show_id)
    state_ok = (
        status == 200
        and isinstance(show, dict)
        and show.get("confirmed") == 4
        and invariant_passes(show)
    )

    passed = status_ok and state_ok
    print(f"[{'PASS' if passed else 'FAIL'}] Per-user limit correctness")
    return passed


async def test_idempotency(
    session: aiohttp.ClientSession,
    base_url: str,
) -> bool:
    print("\n=== IDEMPOTENCY CONCURRENCY ===")

    show_id = await create_show(
        session,
        base_url,
        name=f"idempotency-{uuid.uuid4()}",
        seats=["C1", "C2", "C3", "C4"],
        per_user_limit=4,
    )

    same_user = "idem-user"
    same_key = f"same-key-{uuid.uuid4()}"

    tasks = [
        (same_user, same_key, ["C1"])
        for _ in range(50)
    ]

    results = await run_synchronized_batch(
        session,
        base_url,
        show_id,
        tasks,
    )

    print_distribution(results)

    reservation_ids = {
        str(r.body.get("reservation_id"))
        for r in results
        if isinstance(r.body, dict)
        and r.body.get("reservation_id")
    }

    no_server_errors = (
        server_5xx(results) == 0
        and client_errors(results) == 0
    )
    only_expected_statuses = all(r.status in (200, 201, 409) for r in results)
    one_logical_reservation = len(reservation_ids) == 1

    # Reuse the same key with a different body. This must be 409.
    status, body, _ = await http_request(
        session,
        "POST",
        base_url,
        f"/shows/{show_id}/reserve",
        body={"seats": ["C2"]},
        user_id=same_user,
        idempotency_key=same_key,
    )

    different_body_ok = status == 409

    passed = (
        no_server_errors
        and only_expected_statuses
        and one_logical_reservation
        and different_body_ok
    )

    print(f"Unique reservation IDs: {len(reservation_ids)}")
    print(f"Same-key/different-body status: {status}")
    print(f"[{'PASS' if passed else 'FAIL'}] Idempotency correctness")
    return passed


async def test_multi_seat_atomicity(
    session: aiohttp.ClientSession,
    base_url: str,
) -> bool:
    print("\n=== MULTI-SEAT ATOMICITY ===")

    show_id = await create_show(
        session,
        base_url,
        name=f"atomicity-{uuid.uuid4()}",
        seats=["D1", "D2", "D3", "D4"],
        per_user_limit=4,
    )

    # First reserve D2.
    status, _, _ = await http_request(
        session,
        "POST",
        base_url,
        f"/shows/{show_id}/reserve",
        body={"seats": ["D2"]},
        user_id="occupier",
        idempotency_key=f"occupy-{uuid.uuid4()}",
    )

    if status not in (200, 201):
        print(f"[FAIL] Could not prepare atomicity test: HTTP {status}")
        return False

    # D1 is available but D2 is already confirmed.
    status, _, _ = await http_request(
        session,
        "POST",
        base_url,
        f"/shows/{show_id}/reserve",
        body={"seats": ["D1", "D2"]},
        user_id="atomic-user",
        idempotency_key=f"atomic-{uuid.uuid4()}",
    )

    show_status, show = await get_show(session, base_url, show_id)

    d1 = None
    d2 = None
    if isinstance(show, dict):
        for seat in show.get("seats", []):
            if seat.get("seat") == "D1":
                d1 = seat.get("status")
            elif seat.get("seat") == "D2":
                d2 = seat.get("status")

    passed = (
        status == 409
        and show_status == 200
        and d1 == "AVAILABLE"
        and d2 == "CONFIRMED"
        and isinstance(show, dict)
        and invariant_passes(show)
    )

    print(f"Response status: {status}")
    print(f"D1={d1}, D2={d2}")
    print(f"[{'PASS' if passed else 'FAIL'}] Multi-seat all-or-nothing")
    return passed


async def run_full_burst(
    session: aiohttp.ClientSession,
    base_url: str,
    total_requests: int,
    concurrency: int,
) -> bool:
    print("\n=== FULL BURST ===")
    print(f"Requests: {total_requests:,}")
    print(f"Synchronized concurrency per batch: {concurrency}")

    seats = [
        f"{chr(65 + row)}{seat}"
        for row in range(5)
        for seat in range(1, 21)
    ]
    hot_seats = ["A1", "A2", "A3", "B1", "B2"]
    users = [f"user-{i}" for i in range(100)]

    # Build requests while ensuring ~10% are genuine replays of an earlier
    # request: same user + same key + same seats.
    tasks: list[tuple[str, str, list[str]]] = []
    replay_candidates: list[tuple[str, str, list[str]]] = []

    for i in range(total_requests):
        if replay_candidates and random.random() < 0.10:
            task = random.choice(replay_candidates)
            tasks.append((task[0], task[1], list(task[2])))
            continue

        user_id = random.choice(users)
        key = str(uuid.uuid4())

        if random.random() < 0.50:
            count = random.randint(1, 4)
            chosen = random.sample(hot_seats, min(count, len(hot_seats)))
        else:
            count = random.randint(1, 4)
            chosen = random.sample(seats, count)

        task = (user_id, key, chosen)
        tasks.append(task)
        replay_candidates.append(task)

    started = time.perf_counter()
    results = await run_in_batches(
        session,
        base_url,
        show_id=await create_show(
            session,
            base_url,
            name=f"full-burst-{uuid.uuid4()}",
            seats=seats,
            per_user_limit=4,
        ),
        tasks=tasks,
        concurrency=concurrency,
    )
    elapsed = time.perf_counter() - started

    print(f"Elapsed: {elapsed:.2f}s")
    print(f"Throughput: {len(results) / elapsed:.2f} req/s")
    print_distribution(results)

    # Count confirmed response reservation IDs. Replayed successful responses
    # should point to the same reservation ID; different reservation IDs on the
    # same seat would be a correctness problem, which we also inspect below.
    confirmed_ids = {
        str(r.body.get("reservation_id"))
        for r in results
        if r.status in (200, 201)
        and isinstance(r.body, dict)
        and r.body.get("reservation_id")
    }

    # GET the show used for the burst. The show_id is not retained above, so
    # recreate it from the results isn't possible; this is handled by the
    # caller via the return tuple below.
    return (
        all(r.status in (200, 201, 409) for r in results)
        and server_5xx(results) == 0
        and client_errors(results) == 0
        and len(results) == total_requests
        and len(confirmed_ids) >= 1
    )


async def run_full_burst_with_state(
    session: aiohttp.ClientSession,
    base_url: str,
    total_requests: int,
    concurrency: int,
) -> bool:
    """Full burst with final show-state validation."""
    seats = [
        f"{chr(65 + row)}{seat}"
        for row in range(5)
        for seat in range(1, 21)
    ]
    hot_seats = ["A1", "A2", "A3", "B1", "B2"]
    users = [f"burst-user-{i}" for i in range(100)]

    show_id = await create_show(
        session,
        base_url,
        name=f"full-burst-{uuid.uuid4()}",
        seats=seats,
        per_user_limit=4,
    )

    tasks: list[tuple[str, str, list[str]]] = []
    replay_candidates: list[tuple[str, str, list[str]]] = []

    for _ in range(total_requests):
        if replay_candidates and random.random() < 0.10:
            original = random.choice(replay_candidates)
            tasks.append((original[0], original[1], list(original[2])))
        else:
            user_id = random.choice(users)
            key = str(uuid.uuid4())
            if random.random() < 0.50:
                count = random.randint(1, 4)
                chosen = random.sample(hot_seats, min(count, len(hot_seats)))
            else:
                count = random.randint(1, 4)
                chosen = random.sample(seats, count)

            task = (user_id, key, chosen)
            tasks.append(task)
            replay_candidates.append(task)

    print("\n=== FULL BURST ===")
    print(f"Requests: {total_requests:,}")
    print(f"Synchronized concurrency per batch: {concurrency}")
    print(f"Show ID: {show_id}")

    started = time.perf_counter()
    results = await run_in_batches(
        session,
        base_url,
        show_id,
        tasks,
        concurrency,
    )
    elapsed = time.perf_counter() - started

    print(f"Elapsed: {elapsed:.2f}s")
    print(f"Throughput: {len(results) / elapsed:.2f} req/s")
    print_distribution(results)

    status, show = await get_show(session, base_url, show_id)

    state_ok = (
        status == 200
        and isinstance(show, dict)
        and invariant_passes(show)
    )

    expected_http_statuses = all(r.status in (200, 201, 409) for r in results)
    no_failures = server_5xx(results) == 0 and client_errors(results) == 0
    complete = len(results) == total_requests

    print("\nFinal seats")
    print("-----------")
    if isinstance(show, dict):
        print(f"total:       {show.get('total_seats')}")
        print(f"available:   {show.get('available')}")
        print(f"held:        {show.get('held', 0)}")
        print(f"confirmed:   {show.get('confirmed')}")
        print(
            f"invariant:   {show.get('available')} + "
            f"{show.get('held', 0)} + {show.get('confirmed')} = "
            f"{show.get('total_seats')}"
        )
        print(f"[{'PASS' if invariant_passes(show) else 'FAIL'}] Reconciliation invariant")
    else:
        print(f"Failed to retrieve final state: HTTP {status}: {show}")

    passed = expected_http_statuses and no_failures and complete and state_ok
    print(f"[{'PASS' if passed else 'FAIL'}] Full burst")
    return passed


async def main() -> int:
    base_url = sys.argv[1].rstrip("/") if len(sys.argv) >= 2 else DEFAULT_BASE_URL
    total_requests = int(sys.argv[2]) if len(sys.argv) >= 3 else DEFAULT_TOTAL_REQUESTS
    concurrency = int(sys.argv[3]) if len(sys.argv) >= 4 else DEFAULT_CONCURRENCY

    if total_requests <= 0 or concurrency <= 0:
        print("total_requests and concurrency must be > 0")
        return 1

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    connector = aiohttp.TCPConnector(
        limit=max(concurrency, 50),
        limit_per_host=max(concurrency, 50),
        ttl_dns_cache=300,
    )

    print("=" * 64)
    print("LIVE SEAT RESERVATION REGRESSION")
    print("=" * 64)
    print(f"BASE_URL         = {base_url}")
    print(f"TOTAL_REQUESTS   = {total_requests:,}")
    print(f"CONCURRENCY      = {concurrency}")

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
    ) as session:
        checks = []

        checks.append(await check_health(session, base_url))
        checks.append(await test_hot_seat(session, base_url))
        checks.append(await test_user_limit(session, base_url))
        checks.append(await test_idempotency(session, base_url))
        checks.append(await test_multi_seat_atomicity(session, base_url))
        checks.append(
            await run_full_burst_with_state(
                session,
                base_url,
                total_requests,
                concurrency,
            )
        )

    print("\n" + "=" * 64)
    if all(checks):
        print("ALL REGRESSION TESTS PASSED")
        print("=" * 64)
        return 0

    print("SOME REGRESSION TESTS FAILED")
    print("=" * 64)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted")
        raise SystemExit(130)
