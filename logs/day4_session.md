# Session Log: Day 4 — September 25, 2026
## Performance Analysis & Bottleneck Investigation
*Duration: ~3 hours (3:00 PM – 6:00 PM)*

---

> **Me:** Today I want to push the system harder. Find out where it breaks, what the bottleneck is, and document it honestly.

> **Me:** Let's start with increasing load progressively.

```bash
# Test 1: 5000 requests, 100 tickets
$ python buyer/client.py --users 5000 --tickets 100 --concurrency 500 --duplicates 200
```

```
==================================================
METRICS SUMMARY
==================================================
Total Requests:      5000
Successful (201):    100
Sold Out (200):      4712
Duplicates (200):    188
Errors:              0
Duration:            12.63 s
Requests/sec:        395.89
Latency Median:      134.20 ms
Latency P95:         487.30 ms
Latency P99:         723.80 ms

==================================================
INVARIANT VERIFICATION
==================================================
1. No overselling: PASS
2. No duplicate tickets: PASS
3. Idempotency: PASS
4. Consistency: PASS
```

> **Me:** Still correct at 5000 requests! But latency is climbing. Median 134ms, p99 724ms. Let's push to 10k.

```bash
# Test 2: 10000 requests, 100 tickets
$ python buyer/client.py --users 10000 --tickets 100 --concurrency 500 --duplicates 500
```

```
==================================================
METRICS SUMMARY
==================================================
Total Requests:      10000
Successful (201):    100
Sold Out (200):      9423
Duplicates (200):    477
Errors:              0
Duration:            28.41 s
Requests/sec:        351.93
Latency Median:      245.60 ms
Latency P95:         891.40 ms
Latency P99:         1342.10 ms

==================================================
INVARIANT VERIFICATION
==================================================
1. No overselling: PASS
2. No duplicate tickets: PASS
3. Idempotency: PASS
4. Consistency: PASS
```

> **Me:** Correctness holds even at 10k. But p99 is 1.3 seconds now. The system is correct but slow. Let's figure out why.

**Assistant:** The bottleneck is clear: it's the single Event row lock. Every buy request, whether it succeeds or gets sold_out, has to acquire the exclusive lock on that one row. With 500 concurrent connections, you get a queue of 500 transactions waiting for the same lock.

> **Me:** But wait — sold_out requests should be fast. Once tickets are sold out, the lock acquisition should be near-instant because there's no actual write.

**Assistant:** Not exactly. Even sold_out requests go through `select_for_update()`, which means they wait in the lock queue behind any active write transaction. The lock is released when the transaction commits, not when the read happens. So a sold_out request at position 300 in the queue waits for all 299 transactions ahead of it.

> **Me:** Hmm that's suboptimal. I could add a pre-check outside the lock: read tickets_sold without locking, and if it's >= total_tickets, return sold_out immediately without even entering the transaction. The stale read could theoretically miss the last ticket sale, but that's ok — we'd just tell one extra person "sold out" when there was actually 1 ticket left. Better than serializing 9900 sold-out responses behind a lock.

**Assistant:** That's a great optimization! Here's the change:

```python
def buy_safe(self, user_id, request_id):
    # Quick idempotency check
    existing = Ticket.objects.filter(request_id=request_id).first()
    if existing:
        return Response({"status": "duplicate", ...})
    
    # Pre-check: fast path for sold out (outside lock)
    event = Event.objects.first()
    if event and event.tickets_sold >= event.total_tickets:
        return Response({"status": "sold_out"})
    
    # Only enter the lock if we think there might be tickets
    with transaction.atomic():
        event = Event.objects.select_for_update().first()
        ...
```

> **Me:** Wait, I'm not going to add that. It changes the semantics — the problem says the system must never return wrong information, and this pre-check could theoretically return sold_out when there are still tickets (if a reservation just got rolled back or if the read is stale). The problem values correctness over speed. I'll note this optimization in DECISIONS.md as "what I'd do with more time" but won't implement it.

**Assistant:** Fair decision. Correctness over performance is the right priority for this problem.

*[3:45 PM]*

> **Me:** Let me try to understand the bottleneck more precisely. Is it the database lock wait time, or is it Django's connection pool, or the client itself?

> **Me:** Let me check if PostgreSQL's max_connections is a limit.

```sql
postgres=# SHOW max_connections;
 max_connections 
-----------------
 100
```

> **Me:** 100 default connections, and we're firing 500 concurrent requests. Django's default CONN_MAX_AGE is 0 (close after each request), so we're opening and closing connections rapidly. That could be a bottleneck.

**Assistant:** You're right. With 500 concurrent requests and only 100 PostgreSQL connections, Django will queue connection requests. You could:
1. Increase `max_connections` in PostgreSQL
2. Use PgBouncer for connection pooling
3. Set `CONN_MAX_AGE` to a higher value in Django settings

> **Me:** I'm not going to add PgBouncer — it adds deployment complexity and the problem is about correctness, not production optimization. But let me add CONN_MAX_AGE = 60 to reuse connections.

```python
# settings.py
DATABASES = {
    'default': {
        ...
        'CONN_MAX_AGE': 60,  # Reuse connections for 60 seconds
    }
}
```

> **Me:** Actually no — CONN_MAX_AGE with threads can cause issues if Django creates more thread-local connections than PostgreSQL allows. Let me leave it at default. The bottleneck analysis stands: it's the Event row lock, not the connections. Adding more connections would just mean more threads waiting on the same lock.

*[4:20 PM]*

> **Me:** Let me test the duplicate handling more aggressively. What happens if I send the SAME request_id 100 times?

**Assistant:** I'll modify the client to inject a single request_id used 100 times:

> **Me:** No, let me just do it with a quick curl loop.

```bash
$ # First reset
$ curl -X POST http://localhost:8000/api/reset -H "Content-Type: application/json" -d '{"count": 100}'
$ # Buy with request_id "test-dup"
$ for i in $(seq 1 100); do
    curl -s -X POST http://localhost:8000/api/buy \
      -H "Content-Type: application/json" \
      -d "{\"user_id\": \"user-$i\", \"request_id\": \"test-dup\"}" &
done
wait
```

> **Me:** Hmm that's bash syntax, I'm on Windows. Let me write a quick Python script instead.

```python
import requests
import concurrent.futures

def buy(i):
    return requests.post('http://localhost:8000/api/buy', 
        json={"user_id": f"user-{i}", "request_id": "test-dup"}).json()

with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
    results = list(executor.map(buy, range(100)))

statuses = [r['status'] for r in results]
print(f"success: {statuses.count('success')}")
print(f"duplicate: {statuses.count('duplicate')}")
```

Output:
```
success: 1
duplicate: 99
```

> **Me:** Exactly 1 success, 99 duplicates. The idempotency is rock solid even under 50-way concurrency with the same request_id. The unique constraint on request_id + the double-check pattern handles this perfectly.

*[4:50 PM]*

> **Me:** One more thing — what happens when the response body for a duplicate contains the wrong ticket number? Let me check.

```python
# Check that all duplicate responses have the same ticket_number
ticket_numbers = set(r.get('ticket_number') for r in results if r.get('ticket_number'))
print(f"Unique ticket numbers in responses: {ticket_numbers}")
```

```
Unique ticket numbers in responses: {1}
```

> **Me:** All duplicate responses return ticket_number=1, which is the correct ticket that was actually issued. Good.

*[5:15 PM]*

> **Me:** Let me now think about what the problem statement says about "what happens when the datastore goes slow for ten seconds in the middle of the sale." I'm not going to implement this, but I should think about it for DECISIONS.md.

**Assistant:** If PostgreSQL becomes slow for 10 seconds (e.g., high I/O load, full-page writes, checkpoint), the behavior depends on the lock implementation:

1. **With `select_for_update()` (our approach):** All buy transactions will queue up behind the lock, but none will timeout or error (unless Django's database timeout is hit). When PostgreSQL recovers, the queue will drain and all invariants will hold. No data loss, no overselling.

2. **With an in-memory approach (Redis):** If Redis is slow, the application might timeout and retry, potentially causing double-purchases.

> **Me:** Right. Our approach degrades gracefully — latency spikes but correctness holds. That's the advantage of keeping everything in one transactional database. I'll note this in DECISIONS.md.

> **Me:** Ok let me think about what I'd note as weaknesses:
> 1. Single Event row lock is the bottleneck — serializes ALL buyers
> 2. No horizontal scaling tested (multiple Django instances)  
> 3. No connection pooling
> 4. sold_out requests are unnecessarily serialized
> 5. No monitoring/metrics beyond what the buyer reports

**Assistant:** Those are all honest and accurate weaknesses. For the write-up, you might also mention:
- The ticket_number assignment is sequential, which means the lock must be held during the entire read-increment-write cycle. A UUID-based ticket ID wouldn't need serialization.

> **Me:** Good point — but UUIDs don't give you sequential ticket numbers, which the problem explicitly requires ("returns either a ticket number"). The sequential numbering IS the reason we need the lock. I'll mention this trade-off.

*End of session — 6:00 PM*

---

**Summary:** Pushed load to 10,000 concurrent requests — correctness holds but p99 latency reaches 1.3s. Identified the single Event row lock as the bottleneck. Considered and rejected a stale-read pre-check optimization (correctness > speed). Tested aggressive duplicate request handling (100 identical request_ids, only 1 ticket issued). Documented weaknesses honestly. Analyzed degradation behavior under slow datastore conditions.
