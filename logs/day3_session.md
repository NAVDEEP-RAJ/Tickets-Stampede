# Session Log: Day 3 — September 24, 2026
## Fixed Implementation & Passing Tests
*Duration: ~3 hours (11:00 AM – 2:00 PM)*

---

> **Me:** Today's goal: run the fixed version and get all 4 invariants passing. Then write unit tests.

> **Me:** Start the server without NAIVE_MODE (defaults to false, which uses buy_safe with select_for_update).

```bash
$ python manage.py runserver
```

> **Me:** Fire the same load test that broke the naive version.

```bash
$ python buyer/client.py --users 1000 --tickets 100 --concurrency 200 --duplicates 50
```

**Assistant:** Running...

```
Resetting sale with 100 tickets...
Sale reset successfully.
Generating 1000 user requests...
Injected 50 duplicate requests.
Total requests to fire: 1000
Firing requests...
All requests completed.
Fetching final status...

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
3. Idempotency: PASS
4. Consistency: PASS (tickets_sold=100 matches list length)

ALL INVARIANTS PASSED!
```

> **Me:** All 4 invariants pass. Exactly 100 tickets sold, all unique numbers, idempotency working, consistency check passes. The locking works.

> **Me:** But look at the latency. Median went from 45ms (naive) to 67ms (fixed), p99 from 187ms to 312ms. That's the serialization cost. Every buyer waits in line behind the lock.

**Assistant:** The throughput is still reasonable at ~205 req/s. The latency increase is expected because `select_for_update()` creates a queue: each transaction must wait for the previous one to commit before it can acquire the lock.

> **Me:** 44 out of 50 duplicate request_ids were correctly handled. What happened to the other 6?

**Assistant:** Those 6 likely arrived and were processed as sold_out responses (not as duplicates) because the original request with that request_id also got a sold_out response. If the first request for a given request_id arrived when tickets were already gone, it returned sold_out without creating a Ticket record, so there's nothing in the DB for the duplicate to match against.

> **Me:** Ah right, that makes sense. The idempotency logic only returns "duplicate" if a Ticket was actually created for that request_id. If the first attempt was sold_out, the duplicate also gets sold_out. That's correct behavior — no ticket was ever issued, so there's nothing to deduplicate.

*[11:40 AM]*

> **Me:** Let me save this as the fixed run.

```bash
$ cp buyer/results/run_results.json buyer/results/fixed_run.json
```

> **Me:** Now let's write proper unit tests. I want to test all the basic flows without needing to fire up postgres — Django's test framework uses transactions that get rolled back.

**Assistant:** Here are the tests:

```python
class TicketsAPITests(TestCase):
    def test_reset(self):
        response = self.client.post('/api/reset', 
            json.dumps({"count": 50}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['total_tickets'], 50)
        self.assertEqual(Event.objects.count(), 1)
    
    def test_buy_success(self):
        self.client.post('/api/reset', 
            json.dumps({"count": 10}), content_type='application/json')
        response = self.client.post('/api/buy',
            json.dumps({"user_id": "u1", "request_id": "r1"}),
            content_type='application/json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['ticket_number'], 1)
```

> **Me:** You need to also test sold_out, idempotency, and the consistency between tickets_sold and the actual list.

**Assistant:** Added:

```python
def test_buy_sold_out(self):
    # Reset with 1 ticket, buy it, try buying again
    ...
    self.assertEqual(response2.json()['status'], 'sold_out')

def test_idempotency(self):
    # Buy with same request_id twice
    ...
    self.assertEqual(Ticket.objects.count(), 1)  # Only 1 ticket created

def test_status_consistency(self):
    # Buy 5 tickets, verify count matches list length
    ...
    self.assertEqual(data['tickets_sold'], len(data['tickets']))
```

> **Me:** One issue — these tests run against SQLite by default in Django's test framework. Our `select_for_update()` won't actually lock anything. These tests verify the API contract, not the concurrency behavior. The real concurrency test is the buyer client against Postgres.

**Assistant:** Correct. Django's test runner uses an in-memory SQLite database by default (unless you override `DATABASES` in a test settings file). `select_for_update()` on SQLite silently becomes a no-op — it doesn't raise an error, but it also doesn't lock anything.

These unit tests verify:
1. The API endpoints return correct status codes and response shapes
2. The business logic (sold_out, idempotency) works in the serial case
3. The data model relationships are correct

The concurrent correctness is only testable with the buyer client against PostgreSQL.

> **Me:** Right. And that's fine — the unit tests catch regression bugs, the buyer client catches concurrency bugs. Different layers of testing.

*[12:15 PM]*

> **Me:** Let me run the tests.

```bash
$ python manage.py test tickets
Creating test database for alias 'default'...
......
----------------------------------------------------------------------
Ran 6 tests in 0.342s

OK
Destroying test database for alias 'default'...
```

> **Me:** All 6 pass. Now let me think about edge cases. What happens if someone sends a buy request with no user_id?

```bash
$ curl -X POST http://localhost:8000/api/buy -H "Content-Type: application/json" -d '{"request_id": "test1"}'
{"error": "user_id and request_id are required"}
```

> **Me:** Good, validation works. What about an empty request body?

```bash
$ curl -X POST http://localhost:8000/api/buy -H "Content-Type: application/json" -d '{}'
{"error": "user_id and request_id are required"}
```

> **Me:** And what about calling /buy before /reset?

```bash
$ curl -X POST http://localhost:8000/api/buy -H "Content-Type: application/json" -d '{"user_id": "u1", "request_id": "r1"}'
{"error": "No active sale"}
```

**Assistant:** All edge cases handled correctly. The views check for the existence of an Event before proceeding.

> **Me:** Good. One thing I want to double-check — in buy_safe, when we catch IntegrityError, we query for the existing ticket. But what if the IntegrityError was from the unique_together constraint on (event, ticket_number) rather than from request_id? That would mean two transactions got the same ticket_number, which shouldn't be possible with select_for_update but...

**Assistant:** That's a good defensive question. Under `select_for_update()`, the ticket_number collision can't happen because the Event row lock serializes all ticket_number assignments. The only way to get an IntegrityError is from the `request_id` unique constraint.

However, if you wanted to be extra defensive, you could inspect the IntegrityError to check which constraint was violated. In PostgreSQL, the error message includes the constraint name.

> **Me:** Let's not over-engineer it. The select_for_update makes ticket_number collisions impossible. If we get an IntegrityError, it's request_id. The current handling is correct.

*[1:15 PM]*

> **Me:** Let me also think about what happens with the Django development server vs gunicorn. The dev server is single-threaded by default, right?

**Assistant:** Django's `runserver` uses a multi-threaded WSGI server by default since Django 4.0. It handles concurrent requests with threads. However, for production load testing, you'd want to use gunicorn with multiple workers:

```bash
gunicorn seller.wsgi:application --workers 4 --threads 2 --bind 0.0.0.0:8000
```

> **Me:** The problem says "any language, framework and datastore you like" so Django's dev server is fine for the submission. But let me note in DECISIONS.md that production would use gunicorn. 

> **Me:** Actually wait, does the dev server's threading even trigger our race condition properly? If it's multi-threaded, yes — multiple threads can read the same tickets_sold value simultaneously.

**Assistant:** Yes, Django's `runserver` spawns threads to handle concurrent requests. Each thread gets its own database connection (from Django's connection pool), so `select_for_update()` works correctly — each thread's SELECT will see the FOR UPDATE clause and wait for the lock.

> **Me:** Good. Let me run the buyer one more time with higher load to make sure.

```bash
$ python buyer/client.py --users 2000 --tickets 100 --concurrency 300 --duplicates 100
...
ALL INVARIANTS PASSED!
```

> **Me:** Still passing. Let me commit and we'll push load higher tomorrow.

*End of session — 2:00 PM*

---

**Summary:** Ran the fixed implementation under load — all 4 invariants pass consistently. Wrote 6 unit tests covering reset, buy, sold_out, idempotency, status, and consistency. Explored edge cases (missing fields, no active sale). Verified that Django's dev server threading is sufficient for concurrency testing. Confirmed the fix holds at 2000 concurrent requests.
