# Seat Reservation at Scale — Complete Implementation Plan

## Purpose

Build a production-style JSON HTTP API for assigned-seat reservations that remains correct under heavy concurrency and is observable in real time.

The evaluator will deploy the service from a clean checkout and hit the live API with large concurrent bursts, especially many users competing for the same hot seat. Correctness is more important than feature breadth.

## Recommended Technology

- Java 21
- Spring Boot 3.x
- Spring Web
- Spring JDBC (preferred for explicit SQL and row-count handling)
- PostgreSQL
- Flyway for schema migrations
- Micrometer + Prometheus endpoint
- JUnit 5
- Testcontainers PostgreSQL for integration/concurrency tests
- Docker + Docker Compose
- Python for the burst generator

Keep the production architecture intentionally small:

```text
Client / Burst Tool
        |
        v
Spring Boot API
        |
        v
   PostgreSQL
```

Do not add Redis, Kafka, ZooKeeper, Kubernetes, a separate lock service, or a frontend unless there is a concrete requirement. PostgreSQL should be the system of record and the serialization boundary.

---

# 1. Core Design Principles

1. Never use read-then-write logic for seat allocation.
2. All reservation correctness decisions happen inside one PostgreSQL transaction.
3. Use database constraints and locking instead of Java/in-memory synchronization.
4. Expected business contention must return 4xx, not 5xx.
5. Money is integer minor units (`paise`) everywhere.
6. Identity always comes from the authentication token, never the request body.
7. Multi-seat reservations are all-or-nothing.
8. Normalize requested seats before processing.
9. Maintain one clear source of truth for seat state.
10. Make local execution and deployment use the same Dockerized application.

---

# 2. Functional Scope

## Required endpoints

### Create show

```http
POST /shows
Authorization: Bearer admin
Content-Type: application/json
```

Request:

```json
{
  "name": "friday-night",
  "seats": ["A1", "A2", "A3"],
  "price_paise": 25000,
  "per_user_limit": 4
}
```

`per_user_limit` should be optional and default to `4` if omitted.

Response should contain the show ID and show metadata. All seats must initially be `AVAILABLE`.

### Reserve seats

```http
POST /shows/{showId}/reserve
Authorization: Bearer user-123
Idempotency-Key: some-unique-key
Content-Type: application/json
```

Request:

```json
{
  "seats": ["A12", "A13"]
}
```

Success:

```http
201 Created
```

```json
{
  "reservation_id": "res-123",
  "show_id": "show-123",
  "user_id": "user-123",
  "seats": ["A12", "A13"],
  "amount_paise": 50000,
  "status": "CONFIRMED"
}
```

### Cancel reservation

```http
POST /reservations/{reservationId}/cancel
Authorization: Bearer user-123
```

Only the owner may cancel.

### Show state

```http
GET /shows/{showId}
```

Return every seat's status and aggregate counts.

### Health

```http
GET /health/live
GET /health/ready
```

`/health/ready` must actually verify PostgreSQL reachability and fail with HTTP 503 when the dependency is unavailable.

### Metrics

```http
GET /actuator/prometheus
```

---

# 3. Reservation State Model

Keep the seat model simple.

## Seat states

```text
AVAILABLE
CONFIRMED
```

The take-home allows `AVAILABLE / HELD / CONFIRMED`, but the implementation can use immediate confirmation and therefore never enter `HELD` during the normal reservation flow.

Document this explicitly:

> This implementation confirms seats immediately. `HELD` is reserved for a future payment/temporary-hold workflow and is not used in the current reservation path.

## Reservation states

```text
CONFIRMED
CANCELLED
```

State transitions:

```text
AVAILABLE --reserve--> CONFIRMED
CONFIRMED --cancel--> AVAILABLE
```

Never allow a cancelled reservation to manipulate seats belonging to another reservation.

---

# 4. Database Schema

## 4.1 `shows`

```sql
CREATE TABLE shows (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    price_paise BIGINT NOT NULL CHECK (price_paise >= 0),
    per_user_limit INTEGER NOT NULL CHECK (per_user_limit > 0),
    total_seats INTEGER NOT NULL CHECK (total_seats > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## 4.2 `seats`

```sql
CREATE TABLE seats (
    id UUID PRIMARY KEY,
    show_id UUID NOT NULL REFERENCES shows(id),
    seat_number TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'CONFIRMED')),
    user_id TEXT,
    reservation_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (show_id, seat_number)
);
```

Optional indexes:

```sql
CREATE INDEX idx_seats_show_status
    ON seats(show_id, status);

CREATE INDEX idx_seats_reservation
    ON seats(reservation_id);
```

## 4.3 `reservations`

```sql
CREATE TABLE reservations (
    id UUID PRIMARY KEY,
    show_id UUID NOT NULL REFERENCES shows(id),
    user_id TEXT NOT NULL,
    amount_paise BIGINT NOT NULL CHECK (amount_paise >= 0),
    status TEXT NOT NULL CHECK (status IN ('CONFIRMED', 'CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at TIMESTAMPTZ
);
```

Do not require a globally unique `idempotency_key` here.

## 4.4 `idempotency_keys`

```sql
CREATE TABLE idempotency_keys (
    show_id UUID NOT NULL REFERENCES shows(id),
    user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    reservation_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (show_id, user_id, idempotency_key)
);
```

The key namespace is `(show_id, user_id, idempotency_key)`.

`reservation_id` is nullable during the in-flight transaction but will be populated on successful reservation. Since the idempotency row and reservation are committed together, a committed idempotency row should point to a committed reservation.

## 4.5 `user_show_quotas`

```sql
CREATE TABLE user_show_quotas (
    show_id UUID NOT NULL REFERENCES shows(id),
    user_id TEXT NOT NULL,
    seats_held INTEGER NOT NULL CHECK (seats_held >= 0),
    PRIMARY KEY (show_id, user_id)
);
```

This is a denormalized counter used for efficient and atomic per-user limit enforcement.

Invariant:

```text
user_show_quotas.seats_held
    ==
COUNT(CONFIRMED seats for that user/show)
```

Every reservation and cancellation must update both representations in the same database transaction.

---

# 5. Authentication / Identity

For the take-home, use a deliberately simple bearer-token identity model:

```http
Authorization: Bearer user-123
```

The application treats the token value after `Bearer ` as the authenticated user identity.

For admin operations, use a reserved admin token configured via environment variable, e.g.:

```text
ADMIN_TOKEN
```

Important rule:

> Never accept `user_id` from the reserve or cancel request body.

Even if a client sends:

```json
{
  "user_id": "victim",
  "seats": ["A12"]
}
```

while authenticated as `user-123`, the reservation must belong to `user-123`.

Document that real production authentication would use JWT/OIDC validation; the simple bearer identity is only for the exercise.

---

# 6. Request Canonicalization and Hashing

Idempotency needs to distinguish:

- same key + same logical request -> replay
- same key + different logical request -> 409

Canonicalize the seat list first:

```text
["B1", "A12"]
    ->
["A12", "B1"]
```

Reject duplicate seats in the request, e.g.:

```text
["A12", "A12"]
```

or normalize and reject duplicates explicitly. Rejection is easier to reason about.

Construct a canonical request representation containing at least:

```text
show_id
sorted seats
```

Hash it with SHA-256.

Store the hash in `idempotency_keys.request_hash`.

Call the column `request_hash`, not `seat_hash`, because it represents the entire logical reservation request rather than only the seats.

---

# 7. Final Reservation Transaction

This is the critical part of the implementation.

The entire reservation operation should be one `@Transactional` service method or one explicitly controlled JDBC transaction.

Recommended logical sequence:

```text
BEGIN TRANSACTION
    |
    v
Validate show and request
    |
    v
Insert idempotency row with ON CONFLICT DO NOTHING
    |
    +---- existing same hash --> return existing reservation
    |
    +---- existing different hash --> 409
    |
    v
Acquire PostgreSQL transaction-level advisory lock for (user_id, show_id)
    |
    v
Atomically increment/check user quota
    |
    +---- quota cannot be incremented --> 409 PER_USER_LIMIT
    |
    v
Lock requested seat rows in deterministic order using FOR UPDATE SKIP LOCKED
    |
    +---- not all requested rows available --> ROLLBACK + 409 SEAT_TAKEN
    |
    v
Create reservation record
    |
    v
Update all requested seats to CONFIRMED
    |
    v
Store reservation_id on idempotency row
    |
    v
COMMIT
    |
    v
201 Created
```

The important part is that the seat allocation, quota update, reservation creation, and idempotency record are part of the same atomic transaction.

---

# 8. Idempotency Implementation

Do not use the pattern:

```java
try {
    insert();
} catch (DataIntegrityViolationException e) {
    selectExisting();
}
```

inside the same transaction as the insert.

A PostgreSQL constraint violation can make the transaction unusable unless a savepoint/rollback is used. It is cleaner to avoid using the exception as normal concurrency control.

Instead use:

```sql
INSERT INTO idempotency_keys (
    show_id,
    user_id,
    idempotency_key,
    request_hash
)
VALUES (?, ?, ?, ?)
ON CONFLICT (show_id, user_id, idempotency_key)
DO NOTHING
RETURNING reservation_id, request_hash;
```

### New key

If the insert succeeds:

```text
You own the new idempotency record.
Continue reservation processing.
```

### Existing key

If no row is returned:

```sql
SELECT request_hash, reservation_id
FROM idempotency_keys
WHERE show_id = ?
  AND user_id = ?
  AND idempotency_key = ?;
```

Then:

```text
same request_hash
    -> fetch reservation
    -> return original reservation

request_hash differs
    -> 409 IDEMPOTENCY_KEY_REUSED
```

If supporting concurrent same-key requests, the database unique constraint is the ultimate serialization mechanism. Do not attempt to create two reservations for one key.

---

# 9. Per-User Limit Implementation

Use a PostgreSQL transaction-level advisory lock keyed by `(user_id, show_id)`.

Conceptually:

```sql
SELECT pg_advisory_xact_lock(:user_show_lock_key);
```

Where `user_show_lock_key` is a deterministic 64-bit hash derived from:

```text
user_id + show_id
```

Use a strong deterministic hash. Hash collisions do not break correctness; they can only cause unrelated requests to serialize unnecessarily.

The transaction-level lock is automatically released on commit or rollback.

## Atomic quota increment

Use an atomic UPSERT rather than `SELECT COUNT(*)` followed by a write.

Recommended pattern:

```sql
INSERT INTO user_show_quotas (
    show_id,
    user_id,
    seats_held
)
VALUES (?, ?, ?)
ON CONFLICT (show_id, user_id)
DO UPDATE SET
    seats_held = user_show_quotas.seats_held + EXCLUDED.seats_held
WHERE user_show_quotas.seats_held + EXCLUDED.seats_held <= ?
RETURNING seats_held;
```

Interpretation:

```text
row returned
    -> quota increment succeeded

no row returned
    -> user would exceed per_user_limit
    -> rollback
    -> 409 PER_USER_LIMIT
```

Do not intentionally violate a `CHECK (seats_held <= 4)` constraint and then catch the exception as normal quota logic. Prefer a conditional update so quota rejection is an expected result, not a database error.

---

# 10. Seat Locking and Allocation

Before updating seats, explicitly lock the requested rows.

Normalize the requested seat list first.

Use:

```sql
SELECT seat_number, status
FROM seats
WHERE show_id = ?
  AND seat_number = ANY(?)
ORDER BY seat_number
FOR UPDATE SKIP LOCKED;
```

Why:

- `FOR UPDATE` locks the rows being allocated.
- `ORDER BY seat_number` makes lock acquisition intent deterministic.
- `SKIP LOCKED` avoids waiting behind a hot seat that another transaction is currently processing.

For a request of `A12,A13`, if `A12` is currently locked by another transaction, the query may return only `A13`. Since the number of acquired rows is less than requested, immediately treat the request as a domain conflict:

```text
409 SEAT_TAKEN
```

Do not partially reserve `A13`.

## Verify all seats

After the lock query:

```text
returned rows == requested seats count
AND every row.status == AVAILABLE
```

Otherwise:

```text
ROLLBACK
409 SEAT_TAKEN
```

## Conditional update

Use a second defensive update:

```sql
UPDATE seats
SET status = 'CONFIRMED',
    reservation_id = ?,
    user_id = ?,
    updated_at = now()
WHERE show_id = ?
  AND seat_number = ANY(?)
  AND status = 'AVAILABLE';
```

Verify:

```text
updated_row_count == requested_seats_count
```

If not:

```text
ROLLBACK
409 SEAT_TAKEN
```

The row lock plus conditional state predicate gives strong protection against double allocation.

---

# 11. Multi-Seat Behavior

Choose and document:

> All-or-nothing.

Example:

```text
Request: [A12, A13]
A12 = AVAILABLE
A13 = CONFIRMED
```

Result:

```text
409 SEAT_TAKEN

A12 remains AVAILABLE.
A13 remains CONFIRMED.
No reservation is created.
Quota increment is rolled back.
```

This behavior must hold under concurrency as well.

---

# 12. Deadlock Avoidance

There are potentially multiple row locks in a multi-seat transaction.

Always normalize seats:

```text
Input:  ["B1", "A12"]
Sorted: ["A12", "B1"]
```

Use the same SQL lock order:

```sql
ORDER BY seat_number
```

Do not claim that sorting the Java list alone guarantees database lock order. Make the SQL locking order explicit.

Also keep the overall lock order consistent across operations.

Recommended order:

```text
1. idempotency decision
2. advisory user/show lock
3. seat row locks
4. reservation/seat mutations
```

Cancellation must also use the same user/show advisory lock before changing the quota.

---

# 13. Why the Hot-Seat Race Is Correct

Suppose 500 users concurrently request `A12`.

Conceptually:

```text
500 requests
     |
     v
PostgreSQL
     |
     +--> Request 1 obtains A12 row lock
     |        |
     |        +--> sees AVAILABLE
     |        +--> marks CONFIRMED
     |        +--> COMMIT
     |
     +--> Other requests cannot obtain A12 as an available row
              |
              +--> SKIP LOCKED / state check
              +--> transaction rollback
              +--> 409 SEAT_TAKEN
```

Expected outcome:

```text
exactly 1 confirmed reservation for A12
all other conflicting requests -> 409
0 double sales
0 domain-conflict 500s
```

---

# 14. Why the User Limit Is Correct

Example:

```text
per_user_limit = 4
user = user-1
show = show-1
```

User fires 10 concurrent requests.

Because every request acquires the same transaction-level advisory lock:

```text
(user-1, show-1)
```

only one request at a time evaluates/updates the user's quota.

Quota progression:

```text
0 -> 1 -> 2 -> 3 -> 4
```

The fifth and later requests fail the conditional quota update and return:

```text
409 PER_USER_LIMIT
```

Therefore the committed state can never exceed 4.

---

# 15. Cancellation

Endpoint:

```http
POST /reservations/{reservationId}/cancel
Authorization: Bearer user-123
```

Transaction:

```text
BEGIN
  |
  v
Lock reservation row
  |
  v
Verify reservation.user_id == authenticated user
  |
  +---- no -> 403
  |
  v
Check reservation.status == CONFIRMED
  |
  +---- already cancelled -> 409 or documented idempotent cancellation response
  |
  v
Acquire advisory lock(user_id, show_id)
  |
  v
Lock associated seat rows
  |
  v
Set reservation.status = CANCELLED
  |
  v
Set those seats = AVAILABLE
  |
  v
Decrease user_show_quotas.seats_held
  |
  v
COMMIT
```

A cancelled seat becomes re-bookable immediately after commit.

Never cancel using only a stale `reservation_id` without locking and validating the reservation owner/state.

---

# 16. Show State and Reconciliation

`GET /shows/{id}` should calculate seat counts from `seats`, not from independent counters in application memory.

Example SQL:

```sql
SELECT status, COUNT(*)
FROM seats
WHERE show_id = ?
GROUP BY status;
```

Return:

```json
{
  "id": "show-1",
  "name": "friday-night",
  "price_paise": 25000,
  "total_seats": 100,
  "available": 96,
  "held": 0,
  "confirmed": 4,
  "seats": [
    {"seat": "A1", "status": "AVAILABLE"},
    {"seat": "A2", "status": "CONFIRMED"}
  ]
}
```

For the current implementation:

```text
available + confirmed = total_seats
```

If the response includes `held`, then:

```text
available + held + confirmed = total_seats
```

The API should never independently maintain these counts as mutable state.

---

# 17. Error Model

Expected domain outcomes must be clean 4xx responses.

Recommended mapping:

| Condition | HTTP | Error code |
|---|---:|---|
| Invalid body | 400 | `INVALID_REQUEST` |
| Unknown seat | 400 | `SEAT_NOT_FOUND` |
| Empty seat list | 400 | `INVALID_REQUEST` |
| Duplicate seats in request | 400 | `DUPLICATE_SEAT` |
| Seat already taken | 409 | `SEAT_TAKEN` |
| Per-user limit exceeded | 409 | `PER_USER_LIMIT` |
| Same idempotency key + different request | 409 | `IDEMPOTENCY_KEY_REUSED` |
| Reservation not found | 404 | `RESERVATION_NOT_FOUND` |
| Reservation belongs to another user | 403 | `RESERVATION_NOT_OWNED` |
| Invalid reservation state | 409 | `INVALID_RESERVATION_STATE` |
| Database unavailable | 503 | `DEPENDENCY_UNAVAILABLE` |
| Unexpected bug | 500 | `INTERNAL_ERROR` |

Create a standard response shape:

```json
{
  "code": "SEAT_TAKEN",
  "message": "One or more requested seats are no longer available",
  "request_id": "req-123"
}
```

Use `@RestControllerAdvice` to map known exceptions.

The key requirement is that race outcomes are never emitted as 500.

---

# 18. Metrics

Expose Prometheus-compatible metrics at:

```http
/actuator/prometheus
```

Minimum required metrics:

```text
reservations_confirmed_total
reservations_declined_total{reason="seat-taken"}
reservations_declined_total{reason="per-user-limit"}
reservations_declined_total{reason="idempotent-replay"}
seats_available{show_id="..."}
```

Add useful operational metrics:

```text
reservation_requests_total
reservation_duration_seconds
reservation_db_transaction_seconds
reservations_cancelled_total
reservation_5xx_total
reservation_conflicts_total{reason="..."}
```

Do not use `user_id` as a metric label. It creates high cardinality.

`show_id` is acceptable for a small exercise but should also be monitored for cardinality in a large production system.

---

# 19. Structured Logging

Use JSON logs.

Every request should include a correlation/request ID.

Support incoming:

```http
X-Request-ID: req-123
```

If absent, generate one.

Example:

```json
{
  "timestamp": "2026-10-02T12:30:00Z",
  "level": "INFO",
  "request_id": "req-123",
  "method": "POST",
  "path": "/shows/show-1/reserve",
  "user_id": "user-42",
  "show_id": "show-1",
  "seats": ["A12"],
  "outcome": "SEAT_TAKEN"
}
```

Log the outcome, not excessive internal details or full authorization headers.

---

# 20. Health and Readiness

## Liveness

```http
GET /health/live
```

Should only mean:

> The application process is alive.

No database check.

## Readiness

```http
GET /health/ready
```

Run an actual lightweight dependency check such as:

```sql
SELECT 1;
```

If PostgreSQL is unavailable:

```http
503 Service Unavailable
```

The service should fail closed for writes when its database is unavailable.

---

# 21. Integration Test Strategy

Use Testcontainers PostgreSQL so concurrency behavior is tested against real PostgreSQL semantics.

Do not rely solely on H2.

## Required test cases

### Test 1 — Basic reservation

```text
Reserve A1
=> 201
A1 => CONFIRMED
```

### Test 2 — Same seat concurrency

Launch 100 or 1,000 concurrent requests for A1 with unique users/keys.

Assert:

```text
exactly 1 -> 201
all others -> 409
A1 confirmed exactly once
0 -> 500
```

### Test 3 — 20k burst

Use the external burst script for the full 20,000-request test. CI can run a smaller version such as 1,000 concurrent requests.

### Test 4 — Same idempotency key concurrently

100 concurrent requests:

```text
same user
same show
same key
same seats
```

Assert:

```text
one logical reservation
all replay responses refer to the same reservation
no duplicate seats
```

### Test 5 — Same key, different body

First:

```text
key=abc, seats=[A1]
```

Then:

```text
key=abc, seats=[A2]
```

Assert:

```text
409 IDEMPOTENCY_KEY_REUSED
```

### Test 6 — User limit concurrency

Limit = 4.

One user sends 10 parallel requests for different free seats.

Assert:

```text
confirmed seats <= 4
```

### Test 7 — Multi-seat atomicity

A1 free, A2 taken.

Request A1+A2.

Assert:

```text
409
A1 remains free
A2 remains taken
no reservation created
```

### Test 8 — Identity spoofing

Authenticated token:

```text
Bearer user-123
```

Body contains:

```json
{"user_id":"admin","seats":["A1"]}
```

Assert reservation owner is:

```text
user-123
```

### Test 9 — Cancellation ownership

User A creates reservation.

User B attempts cancellation.

Assert:

```text
403
reservation remains CONFIRMED
```

### Test 10 — Cancel then rebook

```text
User A reserves A1
User A cancels
User B reserves A1
```

Assert second reservation succeeds.

### Test 11 — Quota + cancellation race

Verify a concurrent reserve/cancel workload never produces a quota above the configured limit or below zero.

### Test 12 — Reconciliation

After every stress scenario:

```text
available + confirmed == total
```

and for each user/show:

```text
quota.seats_held == confirmed seats owned by user
```

---

# 22. Burst Generator

Create:

```text
scripts/burst.py
scripts/burst.sh
```

Usage:

```bash
./scripts/burst.sh <BASE_URL> [TOTAL_REQUESTS] [CONCURRENCY]
```

Example:

```bash
./scripts/burst.sh https://your-service.onrender.com 20000 200
```

The script should:

1. Create a fresh test show if needed.
2. Generate many distinct users.
3. Generate unique idempotency keys for normal requests.
4. Generate repeated keys for idempotency replay tests.
5. Create a strong hot-seat storm, e.g. many requests for `A12`.
6. Mix in several hot seats and random seats.
7. Collect HTTP status and domain error codes.
8. Print confirmed / conflict / idempotency / 5xx distribution.
9. Fetch final `GET /shows/{id}`.
10. Validate reconciliation.

Example output:

```text
========================================
BURST RESULT
========================================
Total requests:       20,000
201 confirmed:             8
409 seat-taken:        15,000
409 per-user-limit:       20
409 idempotent-replay:  4,972
5xx:                        0

Final seats
-----------
total:                   100
available:                92
confirmed:                 8

Invariant
---------
92 + 8 = 100
PASS
========================================
```

The exact numbers will vary depending on the generated workload.

---

# 23. Dockerization

Use a multi-stage Dockerfile.

Conceptually:

```text
Build stage:
  Maven + JDK
  mvn package

Runtime stage:
  JRE only
  run application.jar
```

Provide:

```text
Dockerfile
docker-compose.yml
```

Docker Compose should contain:

```text
app
postgres
```

A clean checkout must support:

```bash
docker compose up --build
```

and expose the API on a predictable local port, e.g. `8080`.

---

# 24. Configuration

Use environment variables rather than hardcoded credentials.

Example:

```text
SPRING_DATASOURCE_URL
SPRING_DATASOURCE_USERNAME
SPRING_DATASOURCE_PASSWORD
PORT
ADMIN_TOKEN
```

For local Compose, use a local Postgres configuration.

For deployment, inject the hosted database credentials through the platform's environment configuration.

---

# 25. Flyway Migrations

Use versioned SQL migrations.

Example:

```text
V1__create_shows.sql
V2__create_seats.sql
V3__create_reservations.sql
V4__create_idempotency_keys.sql
V5__create_user_show_quotas.sql
V6__add_indexes.sql
```

Do not rely on `ddl-auto=create` in production.

---

# 26. Project Structure

```text
seat-reservation-service/
|
+-- src/
|   +-- main/
|   |   +-- java/com/example/reservation/
|   |   |   +-- controller/
|   |   |   +-- service/
|   |   |   +-- repository/
|   |   |   +-- dto/
|   |   |   +-- entity/
|   |   |   +-- exception/
|   |   |   +-- security/
|   |   |   +-- metrics/
|   |   |   +-- config/
|   |   |
|   |   +-- resources/
|   |       +-- application.yml
|   |       +-- db/migration/
|   |
|   +-- test/
|
+-- scripts/
|   +-- burst.py
|   +-- burst.sh
|
+-- Dockerfile
+-- docker-compose.yml
+-- pom.xml
+-- README.md
+-- WRITEUP.md
+-- .gitignore
```

---

# 27. Repository and Git Commit Strategy

The assignment explicitly asks for full commit history and incremental development.

Do not build everything and create one final commit.

Suggested commits:

```text
1. initialize spring boot service
2. add postgres and flyway schema
3. implement create show
4. implement show state endpoint
5. add bearer identity handling
6. implement atomic seat reservation
7. add advisory-lock quota enforcement
8. add idempotency with ON CONFLICT
9. add cancellation
10. add domain error handling
11. add health and prometheus metrics
12. add structured request logging
13. add integration concurrency tests
14. add burst generator
15. add docker compose and deployment config
16. improve README and WRITEUP
```

The actual number can vary; the important point is that the history demonstrates real incremental development.

---

# 28. README Plan

README should start with evaluator-critical information.

Recommended structure:

```text
# Seat Reservation at Scale

## Live URL
https://...

## Health
https://.../health/live

## Readiness
https://.../health/ready

## Metrics
https://.../actuator/prometheus

## Local Run
...

## Tests
...

## Burst Test
...

## API
...

## Concurrency Design
...

## Error Codes
...
```

Put the `./scripts/burst.sh ...` command prominently near the top.

---

# 29. WRITEUP.md Plan

Keep the write-up concise and technical.

## 29.1 Atomic decision

Explain that PostgreSQL is the serialization point.

Explain:

```text
FOR UPDATE SKIP LOCKED
+
conditional seat update
+
single transaction
```

and why this prevents double allocation.

## 29.2 Per-user limit

Explain:

```text
transaction-level advisory lock(user, show)
+
atomic quota UPSERT
```

and why concurrent requests from one user cannot exceed the limit.

## 29.3 Idempotency

Explain:

```text
UNIQUE(show_id, user_id, idempotency_key)
+
ON CONFLICT DO NOTHING
+
request_hash
+
reservation_id
```

Explain same-key replay and same-key/different-body conflict.

## 29.4 Multi-seat behavior

State clearly:

```text
all-or-nothing
```

and describe deterministic ordering.

## 29.5 Cancellation

Explain how the reservation, seat state, and quota are updated atomically.

## 29.6 Consistency vs availability

Recommended position:

> The reservation path chooses consistency over availability. If PostgreSQL is unavailable, the service fails closed rather than accepting reservations it cannot durably serialize. During an application/database network partition, writes fail rather than risking duplicate allocation.

## 29.7 Observability

Explain metrics, request IDs, structured logs, readiness, and reconciliation.

## 29.8 2 AM paging

Potential alerts:

```text
5xx rate > 1%
readiness failures
PostgreSQL connection pool exhaustion
database errors/deadlocks
reservation latency above threshold
reconciliation mismatch > 0
```

A reconciliation mismatch should be treated as a critical correctness alert.

## 29.9 AI usage

Be specific and honest. Separate:

```text
AI-directed work
    - generated/reviewed scaffolding
    - suggested SQL/test cases
    - helped inspect edge cases

Human-decided work
    - final locking strategy
    - chosen consistency model
    - exact transaction ordering
    - concurrency invariants
    - deployment decisions
```

Do not claim AI-generated code as independently designed if it was not.

## 29.10 What comes next

Potential follow-ups:

```text
real JWT/OIDC authentication
payment gateway idempotency/outbox
temporary holds with expiry
reservation eventing
rate limiting
load testing with realistic traffic
more granular observability
horizontal scaling and PgBouncer
```

---

# 30. Deployment Plan

Recommended deployment shape:

```text
Public Internet
      |
      v
Hosted Spring Boot service
      |
      v
Hosted PostgreSQL
```

Deployment checklist:

1. Push repository to GitHub.
2. Create PostgreSQL instance.
3. Configure environment variables.
4. Deploy from Dockerfile.
5. Verify cold start.
6. Verify `/health/live`.
7. Verify `/health/ready` while DB is healthy.
8. Verify `/actuator/prometheus`.
9. Create an example show.
10. Run the burst script against the public URL.
11. Capture the final reconciliation and metric/log evidence.
12. Put the public URL and test command in README.

---

# 31. Failure-Mode Checklist

Before submission, explicitly test:

### Database down

Expected:

```text
readiness -> 503
reserve -> 503 / dependency error
no false confirmation
```

### Hot seat

Expected:

```text
1 winner
others 409
0 double sells
```

### Hot user

Expected:

```text
at most per_user_limit confirmed seats
```

### Same idempotency key

Expected:

```text
same response / same reservation ID
```

### Same key, different seats

Expected:

```text
409
```

### Multi-seat partial availability

Expected:

```text
409
no partial booking
```

### Cancellation by another user

Expected:

```text
403
```

### Cancel then rebook

Expected:

```text
new user can successfully reserve released seat
```

### Duplicate requests / retries

Expected:

```text
no extra reservation
no extra quota increment
no extra seat assignment
```

### Unexpected exception

Expected:

```text
5xx only for genuine internal/dependency faults
never for normal seat/user contention
```

---

# 32. Important Implementation Details

## Prefer Spring JDBC for critical SQL

The reservation path has several queries where exact row counts and PostgreSQL-specific features matter.

Spring JDBC makes it straightforward to execute:

- `INSERT ... ON CONFLICT DO NOTHING RETURNING`
- `SELECT ... FOR UPDATE SKIP LOCKED`
- `pg_advisory_xact_lock`
- conditional UPSERT
- `UPDATE` and inspect affected row count

JPA can still be used for non-critical reads, but explicit JDBC is easier to reason about for the concurrency-critical path.

## Use `UUID` for IDs

Reservation and show IDs should be UUIDs or similarly opaque IDs.

## Money

Use `BIGINT` / Java `long` for paise.

Never use `float` or `double`.

## Transaction boundary

The reservation operation must not call an external payment service in the middle of the database transaction. This take-home does not require a real payment gateway.

If real payments were added later, use a payment idempotency key and an outbox/transactional workflow because PostgreSQL cannot atomically commit a local transaction and an external HTTP payment call.

---

# 33. Why This Architecture Is the Final Recommendation

This design deliberately maps each correctness requirement to a concrete PostgreSQL mechanism:

```text
No double booking
    -> row locks + status predicate + one transaction

Per-user limit
    -> advisory lock + atomic quota counter

Idempotency
    -> UNIQUE constraint + ON CONFLICT + request hash

Multi-seat atomicity
    -> one transaction + all-or-nothing validation

Deadlock avoidance
    -> deterministic lock ordering

Identity security
    -> token-derived user ID

Cancellation safety
    -> reservation lock + same user/show lock + atomic seat/quota update

Reconciliation
    -> seats table as source of truth

Zero 5xx for domain races
    -> explicit conflict handling
```

This keeps the system small while making the critical guarantees database-enforced instead of dependent on application timing.

---

# 34. Interview Explanation

The key explanation should be:

> PostgreSQL is the serialization boundary. Advisory locks serialize concurrent reservations for the same user/show so the per-user quota cannot be bypassed. Requested seat rows are locked in deterministic order, with `SKIP LOCKED` used to avoid waiting behind hot-seat contention. Seat allocation and the quota change happen in the same transaction. Idempotency is enforced using a unique `(show_id, user_id, idempotency_key)` constraint and a request hash. Therefore Java never relies on a stale 'seat is free' read, and every correctness property has a concrete database primitive behind it.

Do not say:

> We use a Java lock to prevent double booking.

Do not say:

> We query first and then update.

Do not say:

> Sorting the Java list alone guarantees no deadlocks.

Instead explain that the database transaction and explicit lock order determine correctness.

---

# 35. Final Submission Checklist

```text
[ ] Clean GitHub repository
[ ] Incremental commit history
[ ] Java/Spring Boot service
[ ] PostgreSQL database
[ ] Flyway migrations
[ ] Dockerfile
[ ] docker-compose.yml
[ ] POST /shows
[ ] POST /shows/{id}/reserve
[ ] POST /reservations/{id}/cancel
[ ] GET /shows/{id}
[ ] GET /health/live
[ ] GET /health/ready
[ ] GET /actuator/prometheus
[ ] Token-derived identity
[ ] Atomic seat allocation
[ ] Per-user concurrency-safe limit
[ ] Idempotency
[ ] Same-key/different-body rejection
[ ] All-or-nothing multi-seat behavior
[ ] Cancellation safety
[ ] Structured logs
[ ] Request/correlation IDs
[ ] Prometheus metrics
[ ] Testcontainers integration tests
[ ] Hot-seat concurrency test
[ ] User-limit concurrency test
[ ] Idempotency concurrency test
[ ] Burst script
[ ] README
[ ] WRITEUP.md
[ ] Public deployment
[ ] Live URL verified
[ ] 20k burst against live URL
[ ] 0 unexpected 5xx in burst
[ ] Reconciliation PASS
```

---

# 36. Suggested Execution Order for the One-Day Build

## Phase 1 — Skeleton

Create Spring Boot application, PostgreSQL connection, Flyway, Docker, and health endpoints.

## Phase 2 — Schema and show APIs

Implement show creation and show state retrieval.

## Phase 3 — Reservation correctness

Implement the transaction, advisory lock, quota UPSERT, seat locking, and atomic seat update.

## Phase 4 — Idempotency

Add canonical request hashing and `ON CONFLICT` behavior.

## Phase 5 — Cancellation

Implement owner-only cancellation, seat release, and quota decrement.

## Phase 6 — Error handling

Map all expected races to 409 and dependency failures to 503.

## Phase 7 — Observability

Add Prometheus counters/gauges, structured logs, and request IDs.

## Phase 8 — Tests

Run the focused concurrency integration tests.

## Phase 9 — Burst harness

Run 1k, then 5k, then 10k, then 20k locally.

## Phase 10 — Deployment

Deploy the same Docker image/configuration and run the burst against the public URL.

## Phase 11 — Submission polish

Update README, WRITEUP, Git history, live URL, metrics URL, and test evidence.

---

# Final Architecture Summary

The final recommendation is:

```text
                       HTTP REQUEST
                            |
                            v
                 Token -> authenticated user
                            |
                            v
                     BEGIN TRANSACTION
                            |
                            v
                Idempotency ON CONFLICT
                            |
                    +-------+-------+
                    |               |
                 existing          new
                    |               |
              hash comparison      |
               /          \        |
            same          diff     |
             |             |       |
          replay          409      |
                                    v
                         advisory lock(user, show)
                                    |
                                    v
                            atomic quota UPSERT
                                    |
                              +-----+-----+
                              |           |
                            fail        pass
                              |           |
                             409          v
                              \\    lock seats
                               \\   ORDER BY seat_number
                                \\  FOR UPDATE SKIP LOCKED
                                 \\       |
                                  \\ +----+----+
                                   \\|         |
                                  fail       pass
                                   |           |
                                  409          v
                                          create reservation
                                                |
                                                v
                                           update seats
                                                |
                                                v
                                         store reservation_id
                                                |
                                                v
                                             COMMIT
                                                |
                                                v
                                               201
```

This is the design to implement and defend in the interview.
