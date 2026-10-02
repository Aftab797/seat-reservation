# Concurrency Design & Writeup

This document outlines the design decisions made to ensure correctness, consistency, and idempotency in the Seat Reservation System under heavy concurrent load.

## 1. Atomic Decision (No Double-Selling)

PostgreSQL is the single source of truth and the serialization boundary for the system.
To guarantee that a seat can never be double-sold, we rely on atomic row-level locks via `FOR UPDATE SKIP LOCKED`.

```sql
SELECT seat_number, status FROM seats
WHERE show_id = ? AND seat_number = ANY(?)
ORDER BY seat_number FOR UPDATE SKIP LOCKED
```

This ensures that only one transaction can acquire a lock on a specific seat at a time. The `SKIP LOCKED` directive instantly skips seats locked by other in-flight transactions, allowing us to quickly detect conflicts and return a `409 Seat Taken` error without deadlocking or waiting for locks to release.

## 2. Per-User Quota Limit

To strictly enforce a per-user seat limit under heavy concurrency, we use a transaction-level advisory lock on the combination of `user_id` and `show_id`.

```sql
SELECT pg_advisory_xact_lock(...)
```

This serializes requests from the same user for the same show. It strictly enforces the per-user limit by preventing a user from making parallel requests that might bypass the application-level limit check before the database quota table is updated. Once locked, the `user_show_quotas` table is updated atomically.

## 3. Idempotency

Idempotency is guaranteed using an `idempotency_keys` table with a composite unique constraint on `(show_id, user_id, idempotency_key)`.

When a request arrives, we calculate a `request_hash` (SHA-256 of the sorted requested seats).
- If the `INSERT` succeeds, it's a new request.
- If it fails (conflict), we check the existing `request_hash`.
  - If the hash matches, it's a retry of the identical request. If a reservation is already attached, we return the existing reservation.
  - If the hash differs, the user reused the same idempotency key for a different payload. We return a `409 Conflict`.

## 4. Multi-Seat Behavior

The reservation system follows an **all-or-nothing** behavior for multi-seat requests. If a user requests multiple seats, they either get all of them or none of them.

To prevent deadlocks when locking multiple seats, the application always sorts the requested seat numbers alphabetically before executing the `FOR UPDATE` query.


**Holds:** This implementation does not use a temporary hold state because there is no separate payment or checkout phase in the exercise. A successful reservation atomically transitions a seat directly from AVAILABLE to CONFIRMED. Cancellation transitions it back to AVAILABLE. Therefore, HELD is always 0 in this implementation.

## 5. Cancellation

Cancellation is strictly restricted to the owner of the reservation. The operation runs in a single transaction that:
1. Verifies ownership.
2. Updates the reservation status to `CANCELLED`.
3. Releases the seats (`status = 'AVAILABLE'`).
4. Decrements the user's seat quota in the `user_show_quotas` table.

## 6. Consistency vs Availability

The reservation path prioritizes **Consistency over Availability (CP in CAP theorem)**.
If PostgreSQL is unavailable or there is a network partition between the application and the database, the service fails closed. It will refuse to accept reservations it cannot durably serialize, entirely eliminating the risk of duplicate allocation.

## 7. Observability

The system uses:
- **Micrometer/Prometheus:** To expose business metrics (e.g., `reservations_confirmed_total`, `reservations_declined_total`, `seats_available`) and system metrics via `/actuator/prometheus`.
- **Structured JSON Logging (Logstash):** Console logs are formatted as JSON for easy ingestion into ELK/Datadog.
- **Request IDs:** Passed via the `X-Request-ID` header (or generated automatically) and attached to the SLF4J MDC for request tracing.
- **Health Probes:** Liveness (`/health/live`) and readiness (`/health/ready`) probes ensure traffic is only routed when the database is healthy.

## 8. Potential Alerts (2 AM Paging)

Alerts that would page an engineer:
- **5xx Error Rate > 1%**: Indicates unexpected unhandled exceptions or infrastructure failure.
- **Readiness Failures**: Indicates the application lost connection to the database.
- **PostgreSQL Connection Pool Exhaustion**: Too many slow transactions tying up connections.
- **Database Errors / Deadlocks**: Indicates a breakdown in lock ordering.
- **Reservation Latency > Threshold (e.g., P99 > 500ms)**: Slow queries.
- **Reconciliation Mismatch**: (e.g. `available_seats + confirmed_seats != total_seats`). This is a critical correctness alert.

## 9. AI Usage

This project was built primarily by an AI coding agent via iterative context-aware prompt instructions. The AI was directed to:
- Generate the Spring Boot boilerplate and Flyway schemas.
- Implement the core atomic transaction logic, including `FOR UPDATE SKIP LOCKED` and advisory locks.
- Develop the burst testing Python script.
- Add Micrometer observability and structured logging.
- Construct the `docker-compose.yml` and `Dockerfile`.
