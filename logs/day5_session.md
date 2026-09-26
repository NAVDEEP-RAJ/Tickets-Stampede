# Session Log: Day 5 — September 26, 2026
## Write-up, Cleanup & Final Verification
*Duration: ~2.5 hours (10:00 AM – 12:30 PM)*

---

> **Me:** Last day. I need to write DECISIONS.md, clean up the code, and do a final verification run. Let me start with the write-up.

> **Me:** Draft DECISIONS.md for me. Architecture choices, trade-offs, testing approach, weaknesses, and next steps. Keep it under 2 pages.

**Assistant:** Here's my draft:

```markdown
# Architecture Decisions

## Stack: Django + PostgreSQL
Chose Django with PostgreSQL over FastAPI + Redis or Go + Redis.
The decision was driven by one principle: a single source of truth...
```

> **Me:** Wait, don't say "the decision was driven by one principle" — that's too essay-like. Just say what I chose and why. Be direct. Also, you said "FastAPI + Redis" but I actually considered plain FastAPI without Redis too. Let me rewrite the first section.

The intro should be something like:

"I chose Django + PostgreSQL. The alternative I considered most seriously was FastAPI + PostgreSQL — faster async I/O, but Django's ORM gives me select_for_update() with proper transaction management out of the box, and the concurrency gains from async don't matter when the bottleneck is a database row lock anyway."

**Assistant:** That's much better — concrete and opinionated. Let me revise the full document.

> **Me:** Also in the "what was rejected" section, don't just list things. Explain the actual failure mode of each rejected approach. Like for Redis: "Redis DECR is atomic but if the worker crashes between DECR and writing the ticket to PostgreSQL, you have a ghost decrement — one less ticket available but no record of who got it."

*[10:25 AM]*

> **Me:** Ok let me review what you wrote for the testing section.

**Assistant:**
```markdown
## Testing Approach
- Unit tests verify API contract (6 tests, all passing)
- Load tests with custom asyncio client verify concurrency invariants
- Naive version tested first to prove the buyer catches real bugs
```

> **Me:** Add the actual numbers. "Naive version under 500 concurrent requests: 112 tickets sold for 100 available, 4 duplicate ticket numbers. Fixed version under 10,000 concurrent requests: exactly 100 tickets, 0 violations." Reviewers want to see I actually ran the tests, not that I claim to have.

**Assistant:** Updated. Here's the weaknesses section:

```markdown
## Where It Breaks
- The single Event row lock serializes all buyers. Under 10k concurrent requests,
  p99 latency reaches 1.3 seconds. The lock is the bottleneck, not Django or PostgreSQL.
- sold_out responses are unnecessarily serialized...
```

> **Me:** Good, but add: "The sequential ticket numbering is the root cause of the serialization requirement. If ticket numbers were UUIDs, we could use skip_locked on individual Ticket rows and avoid the counter lock entirely. But the problem requires sequential numbering."

*[10:50 AM]*

> **Me:** Now let me clean up the code. A few things I want to fix:

> **Me:** 1) In buyer/client.py, there's a bug — the latency measurement includes the time waiting for the start_event. It should only measure the actual HTTP request time.

**Assistant:** Looking at the code... actually, the latency measurement is correct. `start_time` is set after `await start_event.wait()`, so it only measures the HTTP request:

```python
async def worker(session, base_url, req_data, start_event, results):
    await start_event.wait()  # Wait for synchronization
    user_id, request_id = req_data
    start_time = time.perf_counter()  # Timer starts AFTER the event
    ...
```

> **Me:** Oh wait, you're right. I misread it. The timer starts after the wait. Good.

> **Me:** 2) In the print output, successful purchases show as "Successful (201)" but sold_out shows as "Sold Out (200)". The status codes should match what the API actually returns. Let me double-check...

The API returns:
- 201 for successful purchase ✓
- 200 for sold_out (with status="sold_out" in body) ✓  
- 200 for duplicate (with status="duplicate" in body) ✓

> **Me:** So the categorization in the client needs to check the response body status field, not just the HTTP status code. For 200 responses, we need to look at whether it's "sold_out" or "duplicate".

**Assistant:** You're right. Let me fix the categorization:

```python
if r["status_code"] == 201:
    successful += 1
elif r["status_code"] == 200:
    body_status = (r.get("response_body") or {}).get("status", "")
    if body_status == "sold_out":
        sold_out += 1
    elif body_status == "duplicate":
        duplicate_errors += 1
```

> **Me:** That's better. Make the change.

*[11:15 AM]*

> **Me:** 3) The README should have clear "run in under 5 minutes" instructions. Let me review it.

**Assistant:** The README covers:
1. Prerequisites (Python 3.10+, PostgreSQL or Docker)
2. Docker setup (`docker-compose up -d db`)
3. Install deps, migrate, runserver
4. Run load test
5. Run naive version
6. Run unit tests

> **Me:** Add a section showing the expected output of a successful run. Reviewers should know what "passing" looks like before they run it.

*[11:30 AM]*

> **Me:** Now final verification. Let me do a clean run from scratch.

```bash
$ docker-compose down -v
$ docker-compose up -d db
$ python manage.py migrate
$ python manage.py runserver
```

In another terminal:
```bash
$ python manage.py test tickets
......
Ran 6 tests in 0.289s
OK
```

```bash
$ python buyer/client.py --users 1000 --tickets 100 --concurrency 200 --duplicates 50
...
ALL INVARIANTS PASSED!
```

```bash
$ python buyer/verify.py
...
ALL INVARIANTS PASSED!
```

> **Me:** Everything passes. Let me also run with NAIVE_MODE to make sure the failing run still fails.

```bash
$ set NAIVE_MODE=true
$ python manage.py runserver
```

```bash
$ python buyer/client.py --users 500 --tickets 100 --duplicates 25
...
SOME INVARIANTS FAILED!
```

> **Me:** Good — naive still oversells, fixed still holds. Both behaviors are consistent and reproducible.

*[12:00 PM]*

> **Me:** Final check — make sure the project structure is clean.

```
Tickets-Stampede/
├── README.md
├── DECISIONS.md
├── requirements.txt
├── docker-compose.yml
├── .gitignore
├── manage.py
├── seller/
│   ├── __init__.py
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── tickets/
│   ├── __init__.py
│   ├── apps.py
│   ├── models.py
│   ├── views.py
│   ├── urls.py
│   ├── admin.py
│   └── tests.py
├── buyer/
│   ├── __init__.py
│   ├── client.py
│   ├── verify.py
│   └── results/
│       ├── naive_run.json
│       └── fixed_run.json
└── logs/
    ├── day1_session.md
    ├── day2_session.md
    ├── day3_session.md
    ├── day4_session.md
    └── day5_session.md
```

> **Me:** Clean. Everything accounted for. Let me do a final git commit.

```bash
$ git add -A
$ git commit -m "Final cleanup: fix buyer response categorization, finalize write-up, add final run results"
```

> **Me:** Done. Submission ready.

*End of session — 12:30 PM*

---

**Summary:** Wrote and refined DECISIONS.md — focused on being concrete and opinionated rather than generic. Fixed a response code categorization bug in the buyer client. Cleaned up README with expected output section. Final verification: all tests pass, all invariants pass on fixed version, naive version correctly fails. Project structure clean and ready for submission.
