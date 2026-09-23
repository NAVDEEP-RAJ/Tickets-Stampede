# Session Log: Day 2 — September 23, 2026
## Naive Implementation & Load Testing — Finding the Bug
*Duration: ~3 hours (2:00 PM – 5:00 PM)*

---

> **Me:** Let's get postgres running and test the naive version. Starting docker.

```bash
$ docker-compose up -d db
Creating network "tickets-stampede_default" with the default driver
Creating tickets-stampede_db_1 ... done
```

> **Me:** Ok postgres is up. Let me run migrations.

```bash
$ python manage.py migrate
Operations to perform:
  Apply all migrations: admin, auth, contenttypes, sessions, tickets
Running migrations:
  Applying contenttypes.0001_initial... OK
  ...
  Applying tickets.0001_initial... OK
```

**Assistant:** Great, the database is ready. Want me to start building the load testing client now?

> **Me:** Yeah. I want a python script using asyncio and aiohttp that fires a configurable number of concurrent requests at /api/buy. It needs to: 1) reset the sale first, 2) fire all requests nearly simultaneously, 3) inject some duplicate request_ids, 4) measure latency, 5) check all 4 invariants by calling /status afterward.

**Assistant:** Here's the buyer client. I'll use `asyncio.Event` to synchronize all requests so they fire at the same time:

```python
import asyncio
import aiohttp
import uuid
import time

async def worker(session, base_url, req_data, start_event, results):
    await start_event.wait()
    user_id, request_id = req_data
    start_time = time.perf_counter()
    try:
        async with session.post(f"{base_url}/buy", 
                                json={"user_id": user_id, "request_id": request_id}) as resp:
            status_code = resp.status
            response_body = await resp.json()
    except Exception as e:
        ...
    latency = time.perf_counter() - start_time
    results.append({...})
```

> **Me:** Why asyncio.Event instead of asyncio.Barrier? Barrier would be more accurate — all coroutines wait until every single one is ready, then they all release at the exact same instant.

**Assistant:** `asyncio.Barrier` was added in Python 3.11 and is indeed the better synchronization primitive for this use case. However, `asyncio.Event` is simpler and works on all Python versions. The practical difference is minimal since the event loop processes all waiters in a tight loop when `event.set()` is called.

> **Me:** Fair enough. Let's use Event for compatibility. But make sure to set the TCPConnector limit high enough — default is 100 and we'll be firing 1000+ requests. Also add the Windows event loop policy thing, I'm on Windows.

```python
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
```

**Assistant:** Good catch. I've added that. Here's the duplicate injection logic:

```python
base_requests = [(str(uuid.uuid4()), str(uuid.uuid4())) for _ in range(args.users - args.duplicates)]
duplicates = []
for _ in range(args.duplicates):
    target = random.choice(base_requests)
    duplicates.append((str(uuid.uuid4()), target[1]))  # new user_id, same request_id
all_requests = base_requests + duplicates
random.shuffle(all_requests)
```

> **Me:** That's right — same request_id but different user_id. This tests whether the system correctly deduplicates on request_id regardless of who's sending it.

*[2:45 PM]*

> **Me:** Ok let's run the naive version. Start the server in naive mode.

```bash
$ set NAIVE_MODE=true
$ python manage.py runserver
```

> **Me:** Now fire the buyer at it.

```bash
$ python buyer/client.py --users 500 --tickets 100 --concurrency 200 --duplicates 25
```

**Assistant:** Running...

```
Resetting sale with 100 tickets...
Sale reset successfully.
Generating 500 user requests...
Injected 25 duplicate requests.
Total requests to fire: 500
Firing requests...
All requests completed.
Fetching final status...

==================================================
METRICS SUMMARY
==================================================
Total Requests:      500
Successful (201):    112
Sold Out (200):      370
Duplicates (200):    18
Errors:              0
Duration:            2.34 s
Requests/sec:        213.68
Latency Median:      45.20 ms
Latency P95:         123.70 ms
Latency P99:         187.40 ms

==================================================
INVARIANT VERIFICATION
==================================================
1. No overselling: FAIL (Sold: 108, Limit: 100, List length: 112)
2. No duplicate tickets: FAIL (Unique: 108 / 112)
3. Idempotency: FAIL
4. Consistency: FAIL (tickets_sold=108 but list has 112)

SOME INVARIANTS FAILED!
```

> **Me:** There it is. 112 tickets sold against 100 available. The race condition in the naive version is exactly what I expected. tickets_sold counter says 108 but the actual ticket list has 112 items — that's because multiple threads read the same counter value, incremented it to the same number, and saved. The save() calls clobbered each other.

**Assistant:** Exactly. Here's the race condition timeline:

```
Thread A: reads tickets_sold = 50
Thread B: reads tickets_sold = 50  (same value!)
Thread A: writes tickets_sold = 51, creates Ticket #51
Thread B: writes tickets_sold = 51, creates Ticket #51  ← DUPLICATE TICKET NUMBER
```

Both threads thought they were selling ticket 51. The counter ended up at 51 instead of 52, and two tickets have the same number.

> **Me:** And 7 of the 25 duplicate request_ids got through as separate purchases. The idempotency check raced too — both threads checked for the request_id, both found nothing, both created a ticket.

**Assistant:** Right. The fix is to serialize all these operations behind `select_for_update()` on the Event row. Once Thread A acquires the lock, Thread B has to wait until A's transaction commits.

> **Me:** I know, that's what buy_safe does. But first, let me save this failing run as evidence.

```bash
$ cp buyer/results/run_results.json buyer/results/naive_run.json
```

*[3:20 PM]*

> **Me:** Actually wait, there's something interesting. Some of the duplicate ticket numbers are: 23, 45, 67, 89. These are scattered across the range, not clustered. That tells me the race window is wide — it's not just a narrow edge case at the boundary, it happens throughout the sale.

**Assistant:** That's a good observation. The race window is proportional to the transaction duration. Since the naive version doesn't use transactions at all (each read, check, and write is a separate auto-committed operation), the window is as wide as the time between the read and the save — which is several milliseconds under concurrency. During those milliseconds, dozens of other threads can read the stale value.

> **Me:** Right. And with 200 concurrent connections, the window is practically guaranteed to be hit on every batch of requests. Ok let me also look at the latency numbers. Median 45ms, p99 187ms. That's actually decent throughput for a naive implementation — no locking overhead. The fixed version will be slower because of serialization.

**Assistant:** Correct. With `select_for_update()`, each buyer has to wait for all previous buyers in the queue to complete. The lock serializes them, trading throughput for correctness. We should expect higher latencies but correct results.

> **Me:** That's the trade-off the problem is testing for. "Correctness under concurrency comes first. Speed only counts once nothing is oversold." Let me write up this observation in DECISIONS.md.

*[3:45 PM]*

> **Me:** Actually, one more thing before we move on. Let me check what happens if I increase concurrency further.

```bash
$ python buyer/client.py --users 2000 --tickets 100 --concurrency 500 --duplicates 100
```

```
==================================================
INVARIANT VERIFICATION
==================================================
1. No overselling: FAIL (Sold: 119, Limit: 100, List length: 143)
```

> **Me:** 143 tickets sold for 100 available! The race condition gets dramatically worse with higher concurrency. Makes sense — more threads in the window, more collisions.

*[4:15 PM]*

> **Me:** Ok let's also write the invariant verifier as a separate script. I want buyer/verify.py that just calls GET /status and checks the 4 invariants independently. This way someone can run it after any test.

**Assistant:** Here's verify.py using the requests library:

```python
import requests
import sys
from collections import defaultdict

def main():
    response = requests.get(f"{args.url}/status")
    final_status = response.json()
    
    # Check all 4 invariants...
    # 1. No overselling
    # 2. No duplicate ticket numbers
    # 3. Idempotency
    # 4. Consistency
```

> **Me:** Wait, you used `requests` (sync library) for verify.py but we don't have it in requirements.txt.

**Assistant:** You're right, let me add it.

> **Me:** I'll add it myself. `requests>=2.31`. Done. Let me commit everything.

*[4:30 PM]*

> **Me:** Let me also save the higher-load naive run. I want to reference both in the write-up.

*End of session — 5:00 PM*

---

**Summary:** Built the buyer load-testing client with asyncio+aiohttp. Ran the naive implementation under load and caught it overselling (112 tickets sold for 100 available). Demonstrated that the race condition worsens with higher concurrency (143 tickets at 500 concurrent). Saved the failing runs as evidence. Built the standalone invariant verifier.
