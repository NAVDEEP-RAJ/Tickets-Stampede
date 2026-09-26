# Architecture Decisions

## Stack: Django + PostgreSQL

I chose Django + PostgreSQL. The alternative I considered most seriously was FastAPI + PostgreSQL — faster async I/O, but Django's ORM gives me `select_for_update()` with proper transaction management out of the box, and the concurrency gains from async don't matter when the bottleneck is a database row lock anyway.

I rejected Redis in the transaction path early. Redis `DECR` is atomic, but if the worker crashes between decrementing Redis and writing the ticket to PostgreSQL, you have a ghost decrement — one less ticket available but no record of who got it. Keeping everything in one transactional database means a crash at any point either commits the full operation or rolls it back completely. One source of truth, no split-brain.

## Concurrency Design

The core lock is `SELECT ... FOR UPDATE` on a single `Event` row. Every buy request acquires an exclusive lock on this row inside `transaction.atomic()`, reads the counter, increments it, creates the ticket, and commits. All concurrent buyers serialize at this lock.

This is correct but slow. Under 10,000 concurrent requests, p99 latency reaches 1.3 seconds because every request — including sold-out responses — waits in the lock queue.

The sequential ticket numbering is the root cause. If ticket numbers were UUIDs, I could use `skip_locked` on individual pre-created Ticket rows and avoid the counter lock entirely. But the problem requires sequential numbering ("`returns either a ticket number`"), so the counter is necessary, and the lock is necessary to protect the counter.

## Idempotency: Three Layers

1. **Outer check** (before the lock): Query for an existing Ticket with the same `request_id`. If found, return it. This is a performance optimization — most duplicate requests arrive well after the original was processed, so we skip the lock entirely.

2. **Inner check** (inside the lock): Same query, but now serialized. If two requests with the same `request_id` arrive simultaneously, both pass the outer check, but only one gets through here.

3. **Database constraint**: `unique=True` on `request_id`. If somehow both inner checks pass (which shouldn't happen with `select_for_update`, but defensive programming), the second `INSERT` fails with `IntegrityError`, which we catch and handle.

## Trade-offs Under the Time Limit

- **Single Event row lock**: Simple and correct, but serializes all buyers. Could shard ticket number ranges across multiple rows to reduce contention. Didn't implement because correctness was the priority and sharding adds complexity.
- **No connection pooling**: PostgreSQL's default 100 connections can be a bottleneck under extreme load. PgBouncer would help but adds deployment complexity.
- **No sold-out fast path**: Could add a stale-read pre-check to short-circuit sold-out responses without locking. Rejected because it trades correctness guarantees for speed, and the problem values correctness.

## Testing

| Scenario | Requests | Tickets | Result |
|----------|----------|---------|--------|
| Naive, 500 concurrent | 500 | 100 | **FAIL**: 112 sold, 4 duplicate ticket numbers |
| Naive, 2000 concurrent | 2000 | 100 | **FAIL**: 143 sold, worse under higher concurrency |
| Fixed, 1000 concurrent | 1000 | 100 | **PASS**: exactly 100 sold, all unique |
| Fixed, 10000 concurrent | 10000 | 100 | **PASS**: exactly 100 sold, p99 = 1.3s |
| Idempotency stress | 100 identical request_ids | 100 | **PASS**: 1 ticket issued, 99 duplicates returned |

Unit tests (6 tests) verify API contract in serial execution. The buyer client verifies concurrent correctness against PostgreSQL.

## Where It Breaks

- **Throughput**: The Event row lock serializes all buyers. Above ~2000 req/s, latency degrades significantly. The lock is the bottleneck, not Django or PostgreSQL.
- **Connection limits**: PostgreSQL default max_connections=100 caps concurrent database operations.
- **Sold-out serialization**: After all tickets are sold, subsequent requests still acquire the lock to read the counter, adding unnecessary latency.
- **No horizontal scaling tested**: Multiple Django instances against the same PostgreSQL would work (the lock is database-level, not application-level), but I haven't tested this.

## What I'd Do With Two More Weeks

1. **Ticket sharding**: Pre-allocate ticket number ranges to multiple Event rows (e.g., Event A handles tickets 1-50, Event B handles 51-100). Reduces lock contention by 2x per shard.
2. **Sold-out fast path**: Cache the sold-out state in application memory (set a flag after the last ticket sells). Subsequent requests skip the database entirely. Risk: stale flag after a reset. Mitigation: reset endpoint clears the flag.
3. **PgBouncer**: Connection pooling for production loads.
4. **Waitlist**: Ticket reservation with 30-second expiry. This turns the counter into a state machine (available → reserved → confirmed/expired) and introduces new race conditions at the expiry boundary.
5. **Multi-instance testing**: Run behind nginx with 3 Django instances, verify invariants hold.
6. **Datastore failure testing**: Kill PostgreSQL mid-sale, bring it back, verify no overselling or lost confirmed sales.
