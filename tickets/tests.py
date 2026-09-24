import json
from django.test import TestCase
from .models import Event, Ticket

class TicketsAPITests(TestCase):
    def test_reset(self):
        response = self.client.post('/api/reset', json.dumps({"count": 50}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['total_tickets'], 50)
        self.assertEqual(Event.objects.count(), 1)
        self.assertEqual(Event.objects.first().total_tickets, 50)

    def test_buy_success(self):
        self.client.post('/api/reset', json.dumps({"count": 10}), content_type='application/json')
        
        response = self.client.post(
            '/api/buy',
            json.dumps({"user_id": "u1", "request_id": "r1"}),
            content_type='application/json'
        )
        
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['status'], 'success')
        self.assertEqual(response.json()['ticket_number'], 1)
        self.assertEqual(Ticket.objects.count(), 1)

    def test_buy_sold_out(self):
        self.client.post('/api/reset', json.dumps({"count": 1}), content_type='application/json')
        
        self.client.post(
            '/api/buy',
            json.dumps({"user_id": "u1", "request_id": "r1"}),
            content_type='application/json'
        )
        
        response2 = self.client.post(
            '/api/buy',
            json.dumps({"user_id": "u2", "request_id": "r2"}),
            content_type='application/json'
        )
        
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(response2.json()['status'], 'sold_out')

    def test_idempotency(self):
        self.client.post('/api/reset', json.dumps({"count": 10}), content_type='application/json')
        
        self.client.post(
            '/api/buy',
            json.dumps({"user_id": "u1", "request_id": "r1"}),
            content_type='application/json'
        )
        
        response2 = self.client.post(
            '/api/buy',
            json.dumps({"user_id": "u1", "request_id": "r1"}),
            content_type='application/json'
        )
        
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(response2.json()['status'], 'duplicate')
        self.assertEqual(Ticket.objects.count(), 1)

    def test_status(self):
        self.client.post('/api/reset', json.dumps({"count": 10}), content_type='application/json')
        self.client.post(
            '/api/buy',
            json.dumps({"user_id": "u1", "request_id": "r1"}),
            content_type='application/json'
        )
        
        response = self.client.get('/api/status')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['tickets_sold'], 1)
        self.assertEqual(data['total_tickets'], 10)
        self.assertEqual(len(data['tickets']), 1)
        self.assertEqual(data['tickets'][0]['user_id'], 'u1')
        self.assertEqual(data['tickets'][0]['request_id'], 'r1')

    def test_status_consistency(self):
        self.client.post('/api/reset', json.dumps({"count": 10}), content_type='application/json')
        for i in range(5):
            self.client.post(
                '/api/buy',
                json.dumps({"user_id": f"u{i}", "request_id": f"r{i}"}),
                content_type='application/json'
            )
            
        response = self.client.get('/api/status')
        data = response.json()
        self.assertEqual(data['tickets_sold'], len(data['tickets']))
        self.assertEqual(data['tickets_sold'], 5)
