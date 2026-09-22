from django.conf import settings
from django.db import transaction, IntegrityError
from rest_framework.views import APIView
from rest_framework.response import Response
from .models import Event, Ticket

class ResetView(APIView):
    def post(self, request):
        count = request.data.get('count', 100)
        Ticket.objects.all().delete()
        Event.objects.all().delete()
        Event.objects.create(total_tickets=count, tickets_sold=0)
        return Response({"status": "reset", "total_tickets": count})

class BuyView(APIView):
    def post(self, request):
        user_id = request.data.get('user_id')
        request_id = request.data.get('request_id')
        
        if not user_id or not request_id:
            return Response({"error": "user_id and request_id are required"}, status=400)
            
        if settings.NAIVE_MODE:
            return self.buy_naive(user_id, request_id)
        else:
            return self.buy_safe(user_id, request_id)

    def buy_naive(self, user_id, request_id):
        # Check for duplicate request_id (but NOT atomically - race condition!)
        existing = Ticket.objects.filter(request_id=request_id).first()
        if existing:
            return Response({"status": "duplicate", "ticket_number": existing.ticket_number}, status=200)
        
        event = Event.objects.first()
        if not event:
            return Response({"error": "No active sale"}, status=400)
        
        # Read-check-write WITHOUT locking - THIS WILL OVERSELL
        if event.tickets_sold >= event.total_tickets:
            return Response({"status": "sold_out"}, status=200)
        
        # Race condition: multiple threads read the same tickets_sold value
        event.tickets_sold += 1
        ticket_number = event.tickets_sold
        event.save()
        
        Ticket.objects.create(
            event=event,
            ticket_number=ticket_number,
            user_id=user_id,
            request_id=request_id
        )
        return Response({"status": "success", "ticket_number": ticket_number}, status=201)

    def buy_safe(self, user_id, request_id):
        # First check idempotency OUTSIDE the lock to avoid unnecessary locking
        existing = Ticket.objects.filter(request_id=request_id).first()
        if existing:
            return Response({"status": "duplicate", "ticket_number": existing.ticket_number}, status=200)
        
        try:
            with transaction.atomic():
                # Lock the Event row - all concurrent buyers serialize here
                event = Event.objects.select_for_update().first()
                if not event:
                    return Response({"error": "No active sale"}, status=400)
                
                # Double-check idempotency inside the lock
                existing = Ticket.objects.filter(request_id=request_id).first()
                if existing:
                    return Response({"status": "duplicate", "ticket_number": existing.ticket_number}, status=200)
                
                if event.tickets_sold >= event.total_tickets:
                    return Response({"status": "sold_out"}, status=200)
                
                event.tickets_sold += 1
                ticket_number = event.tickets_sold
                event.save()
                
                Ticket.objects.create(
                    event=event,
                    ticket_number=ticket_number,
                    user_id=user_id,
                    request_id=request_id
                )
            return Response({"status": "success", "ticket_number": ticket_number}, status=201)
        except IntegrityError:
            # request_id uniqueness violation - another thread created it first
            existing = Ticket.objects.filter(request_id=request_id).first()
            if existing:
                return Response({"status": "duplicate", "ticket_number": existing.ticket_number}, status=200)
            return Response({"error": "Concurrent conflict, please retry"}, status=409)

class StatusView(APIView):
    def get(self, request):
        event = Event.objects.first()
        if not event:
            return Response({"error": "No active sale"}, status=400)
            
        return Response({
            "tickets_sold": event.tickets_sold,
            "total_tickets": event.total_tickets,
            "tickets": [
                {"ticket_number": t.ticket_number, "user_id": t.user_id, "request_id": t.request_id}
                for t in event.tickets.all().order_by('ticket_number')
            ]
        })
