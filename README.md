# Ticket Stampede (Problem 1)

A high-concurrency ticket selling service built with Django REST Framework and PostgreSQL, verified against four strict invariants under load.

## System Overview & Invariants

The service exposes three HTTP endpoints (`POST /reset`, `POST /buy`, `GET /status`) designed to handle thousands of concurrent purchase requests while strictly enforcing four system invariants:

1. **No Overselling**: The service never sells more tickets than available (`tickets_sold <= total_tickets`).
2. **Unique Ticket Numbers**: Every issued ticket receives a unique sequential integer ticket number (`ticket_number`).
3. **Idempotency**: Duplicate requests carrying the same `request_id` yield exactly one ticket without issuing duplicate records.
4. **State Consistency**: The count reported by `/status` (`tickets_sold`) matches the exact length of the issued ticket list.

## Technology Stack & Architecture

- **Backend**: Python 3.10+, Django 4.2+, Django REST Framework
- **Database**: PostgreSQL (Row-level exclusive locking via `SELECT ... FOR UPDATE` inside `transaction.atomic()`)
- **Load Client**: Standalone Python script using `asyncio` and `aiohttp` for concurrent request bursts and barrier synchronization
- **Containerization**: Docker & Docker Compose (`docker-compose.yml`)

## Directory Layout

```
Tickets-Stampede/
├── README.md               # Technical overview and evaluation instructions
├── DECISIONS.md             # Architectural decision record and trade-off analysis
├── requirements.txt         # Python dependency definitions
├── docker-compose.yml       # PostgreSQL service configuration
├── manage.py                # Django administrative entrypoint
├── seller/                  # Django project configuration module
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── tickets/                 # Core ticket domain application
│   ├── models.py            # Event and Ticket database schemas
│   ├── views.py             # ResetView, BuyView (Naive & Safe), StatusView
│   ├── urls.py              # Application routing definitions
│   ├── admin.py             # Django admin registration
│   └── tests.py             # Django unit test suite
├── buyer/                   # Load testing and verification client
│   ├── client.py            # Asynchronous concurrent load generator
│   ├── verify.py            # Standalone invariant checking script
│   └── results/             # Recorded evaluation run logs (JSON)
│       ├── naive_run.json   # Benchmark output of un-locked implementation
│       └── fixed_run.json   # Benchmark output of row-locked implementation
└── logs/                    # Development session logs (Stitched 5-day history)
    ├── day1_session.md      # Architecture design and data modeling
    ├── day2_session.md      # Naive implementation and race condition discovery
    ├── day3_session.md      # Transactional row-locking fix and unit testing
    ├── day4_session.md      # High-load performance analysis & limits
    └── day5_session.md      # Final code refactoring and submission audit
```

## Quick Start Guide

### Prerequisites
- Python 3.10 or higher
- PostgreSQL 14+ or Docker / Docker Desktop

### 1. Database Initialization

Using Docker:
```bash
docker-compose up -d db
```

Or using local PostgreSQL:
```bash
createdb ticket_stampede
```

### 2. Environment Setup & Migrations

```bash
pip install -r requirements.txt
python manage.py migrate
```

### 3. Running the API Server

```bash
python manage.py runserver 8000
```

### 4. Executing the Load Test & Verification

In a separate terminal window, run the buyer client to simulate concurrent load:

```bash
python buyer/client.py --users 1000 --tickets 100 --concurrency 200 --duplicates 50
```

Expected Output:
```
==================================================
METRICS SUMMARY
==================================================
Total Requests:      1000
Successful (201):    100
Sold Out (200):      856
Duplicates (200):    44
Errors:              0
Duration:            4.87 s
Requests/sec:        205.34
Latency Median:      67.30 ms
Latency P95:         198.50 ms
Latency P99:         312.80 ms

==================================================
INVARIANT VERIFICATION
==================================================
1. No overselling: PASS (Sold: 100, Limit: 100, List length: 100)
2. No duplicate tickets: PASS (Unique: 100 / 100)
3. Idempotency (no duplicate request_ids in db): PASS
4. Consistency (tickets_sold matches list length): PASS
```

To run independent verification on `/status`:
```bash
python buyer/verify.py
```

### 5. Demonstrating Naive (Vulnerable) Implementation

The application contains a configurable switch (`NAIVE_MODE`) to demonstrate overselling under race conditions:

```bash
# Set environment variable (Linux/macOS)
export NAIVE_MODE=true

# Set environment variable (Windows PowerShell)
$env:NAIVE_MODE="true"

# Start server in naive mode
python manage.py runserver 8000
```

Execute load test against naive implementation:
```bash
python buyer/client.py --users 500 --tickets 100 --duplicates 25
```

Expected Output:
```
1. No overselling: FAIL (Sold: 108, Limit: 100, List length: 112)
2. No duplicate tickets: FAIL (Unique: 108 / 112)
3. Idempotency: FAIL
4. Consistency: FAIL (tickets_sold=108 but list has 112)
```

### 6. Executing Unit Tests

```bash
python manage.py test tickets
```

## API Endpoint Specification

### `POST /api/reset`
Resets the database state and initializes a fresh event with `count` tickets.
- **Request Body**: `{"count": 100}`
- **Response**: `200 OK` -> `{"status": "reset", "total_tickets": 100}`

### `POST /api/buy`
Attempts to purchase a ticket for a given user and request identifier.
- **Request Body**: `{"user_id": "usr_123", "request_id": "req_abc"}`
- **Response (Success)**: `201 Created` -> `{"status": "success", "ticket_number": 1}`
- **Response (Sold Out)**: `200 OK` -> `{"status": "sold_out"}`
- **Response (Duplicate)**: `200 OK` -> `{"status": "duplicate", "ticket_number": 1}`

### `GET /api/status`
Returns current sales totals and full list of issued tickets.
- **Response**: `200 OK` -> 
  ```json
  {
    "tickets_sold": 100,
    "total_tickets": 100,
    "tickets": [
      {
        "ticket_number": 1,
        "user_id": "usr_123",
        "request_id": "req_abc"
      }
    ]
  }
  ```

## Technical Rationale & Decision Record

See [`DECISIONS.md`](DECISIONS.md) for full technical documentation on:
- Choice of PostgreSQL row-level locks (`SELECT ... FOR UPDATE`) over in-memory alternatives (Redis).
- Three-tier idempotency architecture (outer read check, inner serialized lock check, and database unique constraint).
- Performance bottleneck analysis and sharding strategy for high scale.
