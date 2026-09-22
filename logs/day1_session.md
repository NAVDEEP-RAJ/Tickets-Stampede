# Session Log: Day 1 — September 22, 2026
## Project Setup & Initial Architecture
*Duration: ~2.5 hours (10:15 AM – 12:45 PM)*

---

> **Me:** I need to build a system called Ticket Stampede. 50,000 users trying to buy 100 tickets at the same time. Requirements: never oversell, no duplicate ticket numbers, idempotent duplicate requests, status endpoint always consistent. What stack should I use?

**Assistant:** This is a classic concurrency problem! For high-throughput scenarios where thousands of requests hit simultaneously, a fast in-memory store like Redis is usually the best choice. We could use Redis to maintain an atomic counter for the tickets and then asynchronously persist to PostgreSQL. I'd suggest FastAPI + Redis + PostgreSQL.

> **Me:** No. I don't want Redis in the transaction path. If the worker crashes after decrementing Redis but before writing to Postgres, we have a phantom ticket — sold in Redis, absent in the DB. That's exactly the kind of split-brain failure this problem is testing for. Let's use Django + PostgreSQL only. One source of truth. PostgreSQL has row-level locking, that's sufficient.

**Assistant:** Fair point — that's a stronger consistency guarantee. PostgreSQL's `SELECT ... FOR UPDATE` gives us real row-level exclusive locks within a transaction. Let me set up the Django project.

```bash
django-admin startproject seller .
python manage.py startapp tickets
```

> **Me:** Good. Now let's design the models. I'm thinking two models: Event (holds the counter) and Ticket (one row per sold ticket). The Event model is the lock target — we'll `select_for_update()` on it.

**Assistant:** Here's what I'd suggest:

```python
class Event(models.Model):
    name = models.CharField(max_length=100)
    total_tickets = models.IntegerField()
    tickets_sold = models.IntegerField(default=0)

class Ticket(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    user_id = models.CharField(max_length=100)
    ticket_number = models.IntegerField()
```

> **Me:** Close but missing the critical piece. We need `request_id` on the Ticket for idempotency, and it needs a unique constraint at the DB level. The application code can check for duplicates, but the DB constraint is the ultimate guarantee — if two threads race past the application check, the second INSERT will fail with IntegrityError. Also drop the `name` field from Event, we don't need it. Add `created_at` timestamps on both models.

**Assistant:** You're right, the database-level unique constraint is the last line of defense. Here's the updated design:

```python
class Event(models.Model):
    total_tickets = models.IntegerField(default=100)
    tickets_sold = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

class Ticket(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='tickets')
    ticket_number = models.IntegerField()
    user_id = models.CharField(max_length=255)
    request_id = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('event', 'ticket_number')]
        indexes = [
            models.Index(fields=['request_id']),
            models.Index(fields=['user_id']),
        ]
```

> **Me:** Why do you have both `unique=True` on request_id AND an index on request_id? The unique constraint already creates an index.

**Assistant:** You're right, that's redundant. The `unique=True` on `request_id` automatically creates a B-tree index in PostgreSQL. I'll remove the explicit index.

> **Me:** Actually, keep it. Django's ORM sometimes doesn't use the unique constraint's index for non-equality lookups, and the explicit index makes the intent clear to anyone reading the code. It's harmless — Postgres is smart enough not to create a duplicate index. Let's move on to the endpoints.

*[10:52 AM]*

> **Me:** Three endpoints: `POST /api/reset`, `POST /api/buy`, `GET /api/status`. Use DRF APIView, not ViewSets — I want the logic to be explicit, not hidden behind router magic.

**Assistant:** Here's the URL configuration:

```python
# tickets/urls.py
from django.urls import path
from .views import ResetView, BuyView, StatusView

urlpatterns = [
    path('reset', ResetView.as_view(), name='reset'),
    path('buy', BuyView.as_view(), name='buy'),
    path('status', StatusView.as_view(), name='status'),
]

# seller/urls.py
from django.urls import path, include
urlpatterns = [
    path('api/', include('tickets.urls')),
]
```

> **Me:** Good. No trailing slashes — the problem statement shows the endpoints without them. Now let's write the views. Start with reset — it should be simple. Delete everything, create a fresh Event.

**Assistant:** Here's the reset view:

```python
class ResetView(APIView):
    def post(self, request):
        count = request.data.get('count', 100)
        Ticket.objects.all().delete()
        Event.objects.all().delete()
        Event.objects.create(total_tickets=count, tickets_sold=0)
        return Response({"status": "reset", "total_tickets": count})
```

> **Me:** That's fine for now but I want to wrap the delete+create in a transaction so we don't end up in a state where tickets are deleted but the event isn't. Actually, for this project the reset is only called before tests, so it's not a concurrency concern. Let's move on to the buy endpoint — this is the hard part.

*[11:15 AM]*

> **Me:** I want to build this in two stages. First, a deliberately broken "naive" version that does read-check-write without any locking. Then the fixed version. I'll toggle between them with an env var NAIVE_MODE. The point is to demonstrate the buyer catching the naive version overselling.

**Assistant:** Smart approach — showing the failure mode first makes the fix meaningful. Here's the naive version:

```python
def buy_naive(self, user_id, request_id):
    existing = Ticket.objects.filter(request_id=request_id).first()
    if existing:
        return Response({"status": "duplicate", "ticket_number": existing.ticket_number})
    
    event = Event.objects.first()
    if event.tickets_sold >= event.total_tickets:
        return Response({"status": "sold_out"})
    
    event.tickets_sold += 1
    ticket_number = event.tickets_sold
    event.save()
    
    Ticket.objects.create(
        event=event, ticket_number=ticket_number,
        user_id=user_id, request_id=request_id
    )
    return Response({"status": "success", "ticket_number": ticket_number}, status=201)
```

The race condition is between the read (`event.tickets_sold`) and the write (`event.save()`). Under concurrency, multiple threads will read the same `tickets_sold` value, all think there are tickets left, and all increment and save — resulting in overselling.

> **Me:** Right. And the idempotency check has the same race condition — two requests with the same request_id can both pass the `filter().first()` check if they arrive at the exact same time. Good. Now write the fixed version.

**Assistant:** Here's the corrected version using `select_for_update()`:

```python
def buy_safe(self, user_id, request_id):
    # Quick idempotency check outside the lock
    existing = Ticket.objects.filter(request_id=request_id).first()
    if existing:
        return Response({"status": "duplicate", "ticket_number": existing.ticket_number})
    
    try:
        with transaction.atomic():
            event = Event.objects.select_for_update().first()
            
            # Double-check idempotency INSIDE the lock
            existing = Ticket.objects.filter(request_id=request_id).first()
            if existing:
                return Response({"status": "duplicate", "ticket_number": existing.ticket_number})
            
            if event.tickets_sold >= event.total_tickets:
                return Response({"status": "sold_out"})
            
            event.tickets_sold += 1
            ticket_number = event.tickets_sold
            event.save()
            
            Ticket.objects.create(
                event=event, ticket_number=ticket_number,
                user_id=user_id, request_id=request_id
            )
        return Response({"status": "success", "ticket_number": ticket_number}, status=201)
    except IntegrityError:
        existing = Ticket.objects.filter(request_id=request_id).first()
        if existing:
            return Response({"status": "duplicate", "ticket_number": existing.ticket_number})
        return Response({"error": "Concurrent conflict, please retry"}, status=409)
```

> **Me:** Why do you check idempotency twice — once outside and once inside the lock?

**Assistant:** The outer check is a performance optimization. Most duplicate requests arrive well after the original has been processed, so we can return the cached result without acquiring the lock at all. This avoids unnecessary serialization.

The inner check is the correctness guarantee. If two requests with the same `request_id` arrive at the exact same time, both pass the outer check, but only one gets through the inner check (since they serialize on the `select_for_update` lock).

The `IntegrityError` catch is the ultimate fallback — if somehow both inner checks pass (which shouldn't happen with `select_for_update`, but defensive programming), the database's unique constraint on `request_id` will reject the second INSERT.

> **Me:** That's the right reasoning. Three layers of defense: outer check (performance), inner check (correctness), DB constraint (belt and suspenders). Ship it.

*[11:48 AM]*

> **Me:** Now the status endpoint. Simple — return the event state and all tickets.

**Assistant:**
```python
class StatusView(APIView):
    def get(self, request):
        event = Event.objects.first()
        if not event:
            return Response({"error": "No active sale"}, status=400)
        
        tickets = event.tickets.all().order_by('ticket_number')
        return Response({
            "tickets_sold": event.tickets_sold,
            "total_tickets": event.total_tickets,
            "tickets": [
                {"ticket_number": t.ticket_number, "user_id": t.user_id, "request_id": t.request_id}
                for t in tickets
            ]
        })
```

> **Me:** Good. Now settings.py — use PostgreSQL, not SQLite. SQLite doesn't support real row-level locking, so our `select_for_update()` would silently degrade to full-database locking. Add django-cors-headers, DRF, and set ALLOWED_HOSTS to ['*'] for development. Add the NAIVE_MODE toggle.

*[12:05 PM]*

**Assistant:** Here's the settings:

```python
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME', 'ticket_stampede'),
        'USER': os.environ.get('DB_USER', 'postgres'),
        'PASSWORD': os.environ.get('DB_PASSWORD', 'postgres'),
        'HOST': os.environ.get('DB_HOST', 'localhost'),
        'PORT': os.environ.get('DB_PORT', '5432'),
    }
}

NAIVE_MODE = os.environ.get('NAIVE_MODE', 'false').lower() == 'true'
```

> **Me:** Perfect. Also add a docker-compose.yml with just a postgres service so reviewers can spin it up with one command. And let's write unit tests — test reset, buy, sold_out, idempotency, status, and consistency.

**Assistant:** I'll create `docker-compose.yml` and `tickets/tests.py`.

*[12:30 PM]*

> **Me:** Let me also create the requirements.txt. We need: django, djangorestframework, psycopg2-binary, aiohttp (for the buyer client tomorrow), gunicorn, django-cors-headers, requests.

**Assistant:** Done. I've created all the base files. Want me to run migrations?

> **Me:** Not yet — I don't have Postgres running locally. I'll spin up Docker tomorrow and test then. For now let's make sure the project structure is clean and commit what we have.

*End of session — 12:45 PM*

---

**Summary:** Set up Django project structure, designed Event/Ticket models with idempotency constraints, implemented naive (broken) and safe (locking) buy endpoints, status endpoint, reset endpoint. Rejected Redis in favor of single PostgreSQL source of truth. Three layers of idempotency defense: outer check, inner check under lock, DB unique constraint.
