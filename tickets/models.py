from django.db import models

class Event(models.Model):
    total_tickets = models.IntegerField(default=100)
    tickets_sold = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f'Event {self.id}: {self.tickets_sold}/{self.total_tickets} sold'

class Ticket(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='tickets')
    ticket_number = models.IntegerField()
    user_id = models.CharField(max_length=255)
    request_id = models.CharField(max_length=255, unique=True)  # Enforces idempotency at DB level
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = [('event', 'ticket_number')]
        indexes = [
            models.Index(fields=['request_id']),
            models.Index(fields=['user_id']),
        ]
    
    def __str__(self):
        return f'Ticket #{self.ticket_number} -> {self.user_id}'
