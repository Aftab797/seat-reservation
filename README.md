# Seat Reservation at Scale

A production-style JSON HTTP API for assigned-seat reservations that remains correct under heavy concurrency and is observable in real time.

## Live URL
https://seat-reservation-production-ee5f.up.railway.app

Live testing script:
```bash
./scripts/burst.sh https://seat-reservation-production-ee5f.up.railway.app 20000 200
```


## Burst Test (Load testing)

A python script is provided to burst the API with parallel requests.

**Prerequisites:** Python 3.11+, `aiohttp`, `requests`

```bash
python3 -m venv venv
source venv/bin/activate
pip install aiohttp requests

./scripts/burst.sh http://localhost:8080 20000 200
```

## Local Run

Requires Docker and Docker Compose.

```bash
docker-compose up --build -d
```

### Health
`http://localhost:8080/health/live`

### Readiness
`http://localhost:8080/health/ready`

### Metrics (Prometheus)
`http://localhost:8080/actuator/prometheus`

## API Endpoints

- `POST /shows` - Create a show
- `GET /shows/{id}` - Get a show with seat status
- `POST /shows/{id}/reserve` - Reserve seats
- `POST /reservations/{id}/cancel` - Cancel a reservation

See `WRITEUP.md` for concurrency design, idempotency strategy, error codes, and technical choices.

