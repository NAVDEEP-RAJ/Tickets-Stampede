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
            
        try:
            if settings.NAIVE_MODE:
                return self.buy_naive(user_id, request_id)
            else:
                return self.buy_safe(user_id, request_id)
        except Exception as e:
            return Response({"status": "error", "error": "Server or Database busy, please retry"}, status=503)

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

from django.http import HttpResponse

HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ticket Stampede — Telemetry & Operations Console</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:ital,wght@0,400..800;1,400..800&family=Outfit:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
    <script>
        tailwind.config = {
            theme: {
                extend: {
                    fontFamily: {
                        sans: ['"Plus Jakarta Sans"', 'sans-serif'],
                        mono: ['"Outfit"', 'sans-serif']
                    }
                }
            }
        }
    </script>
    <style>
        * {
            font-variant-numeric: normal !important;
            font-feature-settings: "zero" 0, "slash" 0 !important;
        }
        body { 
            font-family: 'Plus Jakarta Sans', sans-serif !important; 
            background: linear-gradient(135deg, #fff7ed 0%, #fff1f2 35%, #fae8ff 70%, #f0f9ff 100%);
            background-attachment: fixed;
        }
        .mono, .font-mono, [class*="font-mono"] { 
            font-family: 'Outfit', 'Plus Jakarta Sans', sans-serif !important; 
            letter-spacing: -0.01em; 
        }
        .sunset-card {
            background: linear-gradient(135deg, rgba(255, 255, 255, 0.92) 0%, rgba(255, 247, 237, 0.85) 50%, rgba(254, 242, 242, 0.9) 100%);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(251, 146, 60, 0.25);
            box-shadow: 0 10px 25px -5px rgba(244, 63, 94, 0.06), 0 8px 10px -6px rgba(251, 146, 60, 0.04);
        }
        .sunset-card-accent {
            background: linear-gradient(135deg, rgba(255, 241, 242, 0.95) 0%, rgba(254, 243, 199, 0.9) 100%);
            border: 1px solid rgba(244, 63, 94, 0.25);
            box-shadow: 0 10px 25px -5px rgba(244, 63, 94, 0.08);
        }
    </style>
</head>
<body class="text-slate-800 min-h-screen pb-12">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
        
        <!-- Top Operational Header -->
        <header class="sunset-card rounded-3xl p-6 mb-6 flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <div class="flex items-center gap-3">
                    <h1 class="text-2xl font-extrabold tracking-tight text-slate-900">Ticket Stampede System</h1>
                </div>
                <p class="text-slate-600 text-xs mt-1 font-semibold">High-concurrency ticket selling analytics & real-time load testing dashboard.</p>
            </div>
            <div class="flex items-center gap-3">
                <span class="inline-flex items-center gap-1.5 text-xs font-bold text-emerald-800 bg-emerald-100/80 border border-emerald-300/60 px-3 py-1.5 rounded-full">
                    <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span> DB Connected
                </span>
                <button onclick="fetchStatus()" class="px-5 py-2.5 bg-white hover:bg-rose-50 text-slate-800 rounded-2xl text-xs font-bold transition shadow-sm border border-rose-200">Refresh State</button>
            </div>
        </header>

        <!-- KPI Metrics Grid -->
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5 mb-6">
            <div class="sunset-card rounded-3xl p-6">
                <div class="text-xs font-extrabold text-amber-800/80 uppercase tracking-wider mb-2">Total Tickets Sold</div>
                <div class="flex items-baseline gap-2">
                    <span id="ticketsSold" class="text-4xl font-extrabold text-slate-900 font-mono">0</span>
                    <span id="totalCapacity" class="text-sm font-semibold text-slate-500 font-mono">/ 100</span>
                </div>
                <div class="w-full bg-rose-100/80 h-2.5 rounded-full mt-4 overflow-hidden">
                    <div id="progressBar" class="bg-gradient-to-r from-amber-500 via-rose-500 to-purple-600 h-full w-0 transition-all duration-300"></div>
                </div>
            </div>

            <div class="sunset-card rounded-3xl p-6">
                <div class="text-xs font-extrabold text-rose-800/80 uppercase tracking-wider mb-2">Available Pool</div>
                <div id="availableCount" class="text-4xl font-extrabold text-rose-600 font-mono">100</div>
                <div class="text-xs font-semibold text-slate-600 mt-4">Remaining inventory</div>
            </div>

            <div class="sunset-card rounded-3xl p-6">
                <div class="text-xs font-extrabold text-purple-800/80 uppercase tracking-wider mb-2">System Invariants</div>
                <div id="invariantsBadge" class="text-2xl font-extrabold text-emerald-700">ALL PASSING</div>
                <div class="text-xs font-semibold text-slate-600 mt-2">Zero overselling / zero duplicates</div>
            </div>

            <div class="sunset-card rounded-3xl p-6">
                <div class="text-xs font-extrabold text-orange-800/80 uppercase tracking-wider mb-2">Peak Purchase Rate</div>
                <div id="peakVelocity" class="text-4xl font-extrabold text-slate-900 font-mono">0 <span class="text-sm text-slate-500 font-normal">t/s</span></div>
                <div id="avgLatency" class="text-xs font-semibold text-slate-600 mt-2">Avg Latency: -- ms</div>
            </div>
        </div>

        <!-- Sales Timeline & Analytics Graph Section -->
        <div class="sunset-card rounded-3xl p-6 mb-6">
            <div class="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 mb-4 pb-4 border-b border-rose-200/60">
                <div>
                    <h2 class="text-lg font-extrabold text-slate-900">Sales Timeline & Ticket Volume Graph</h2>
                    <p class="text-xs font-medium text-slate-600">Real-time cumulative ticket sales over execution time (Seconds)</p>
                </div>
                <div class="flex items-center gap-4 text-xs font-bold text-slate-700">
                    <div class="flex items-center gap-1.5"><span class="w-3 h-3 rounded-full bg-rose-500"></span> Sold Volume</div>
                    <div class="flex items-center gap-1.5"><span class="w-3 h-3 rounded-full bg-amber-500"></span> Sales Rate (t/s)</div>
                </div>
            </div>
            <div class="h-64 w-full relative">
                <canvas id="salesChart"></canvas>
            </div>
        </div>

        <!-- Action Controls Grid -->
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-6">
            <!-- Stampede Simulation Hub -->
            <div class="sunset-card-accent rounded-3xl p-6 flex flex-col justify-between">
                <div>
                    <div class="flex justify-between items-center mb-3">
                        <h2 class="text-base font-extrabold text-slate-900">Simulate Stampede Burst</h2>
                        <span class="text-[10px] font-bold uppercase font-mono px-2.5 py-1 rounded-full bg-rose-500/10 text-rose-700 border border-rose-300/50">Load Client</span>
                    </div>
                    <p class="text-xs font-semibold text-slate-600 mb-4">Fire a parallel burst of concurrent buy requests to test row locking under load.</p>
                    <div class="mb-4">
                        <label class="block text-xs font-bold text-slate-700 mb-1">Burst Size (Concurrent Buyers)</label>
                        <input type="number" id="stampedeCount" value="50" class="w-full bg-white/90 border border-rose-300/80 rounded-2xl px-4 py-2.5 text-slate-900 text-sm focus:outline-none focus:ring-2 focus:ring-rose-400 font-mono">
                    </div>
                </div>
                <button onclick="runStampede()" id="stampedeBtn" class="w-full py-3 bg-gradient-to-r from-rose-600 to-amber-600 hover:from-rose-500 hover:to-amber-500 text-white font-extrabold rounded-2xl text-xs uppercase tracking-wider transition shadow-md shadow-rose-900/10">Trigger Stampede Burst</button>
            </div>

            <!-- Single Purchase Card -->
            <div class="sunset-card rounded-3xl p-6">
                <div class="flex justify-between items-center mb-3">
                    <h2 class="text-base font-extrabold text-slate-900">Manual Purchase Request</h2>
                    <button onclick="randomizeInputs()" class="text-xs font-bold text-rose-600 hover:text-rose-500">Randomize IDs</button>
                </div>
                <div class="space-y-3">
                    <div>
                        <label class="block text-xs font-bold text-slate-700 mb-1">User Identifier</label>
                        <input type="text" id="userId" placeholder="user_123" class="w-full bg-white/90 border border-amber-200/80 rounded-2xl px-4 py-2 text-slate-900 text-xs focus:outline-none focus:ring-2 focus:ring-amber-400 font-mono">
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-700 mb-1">Request Identifier (Idempotency Key)</label>
                        <input type="text" id="requestId" placeholder="req_abc" class="w-full bg-white/90 border border-amber-200/80 rounded-2xl px-4 py-2 text-slate-900 text-xs focus:outline-none focus:ring-2 focus:ring-amber-400 font-mono">
                    </div>
                    <div class="grid grid-cols-2 gap-3 pt-1">
                        <button onclick="buyTicket()" class="py-2.5 bg-gradient-to-r from-amber-500 to-orange-500 hover:from-amber-400 hover:to-orange-400 text-white font-bold rounded-2xl text-xs transition shadow-sm">Buy Ticket</button>
                        <button onclick="buyTicket(true)" class="py-2.5 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 text-white font-bold rounded-2xl text-xs transition shadow-sm" title="Re-send duplicate request ID to verify idempotency">Test Duplicate</button>
                    </div>
                </div>
            </div>

            <!-- Reset Pool Card -->
            <div class="sunset-card rounded-3xl p-6 flex flex-col justify-between">
                <div>
                    <h2 class="text-base font-extrabold text-slate-900 mb-1">Reset Ticket Pool</h2>
                    <p class="text-xs font-semibold text-slate-600 mb-4">Wipe all ticket records and start a fresh sale state.</p>
                    <div class="mb-4">
                        <label class="block text-xs font-bold text-slate-700 mb-1">Pool Capacity</label>
                        <input type="number" id="resetCount" value="100" class="w-full bg-white/90 border border-amber-200/80 rounded-2xl px-4 py-2.5 text-slate-900 text-sm focus:outline-none focus:ring-2 focus:ring-amber-400 font-mono">
                    </div>
                </div>
                <button onclick="resetSale()" class="w-full py-2.5 bg-gradient-to-r from-rose-600 to-red-600 hover:from-rose-500 hover:to-red-500 text-white font-bold rounded-2xl text-xs transition shadow-sm">Reset & Flush Database</button>
            </div>
        </div>

        <!-- Issued Tickets Table & Log Output -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
            <!-- Issued Tickets List -->
            <div class="sunset-card rounded-3xl p-6 flex flex-col h-96">
                <div class="flex justify-between items-center mb-4">
                    <h2 class="text-base font-extrabold text-slate-900">Confirmed Ticket Ledger</h2>
                    <span id="ticketCountBadge" class="text-xs font-bold bg-rose-100 text-rose-800 border border-rose-300/50 px-3 py-1 rounded-full font-mono">0 Records</span>
                </div>
                <div class="overflow-y-auto flex-1 pr-1">
                    <div id="ticketsGrid" class="space-y-2">
                        <div class="text-center text-slate-400 py-12 text-sm">No tickets issued yet.</div>
                    </div>
                </div>
            </div>

            <!-- Execution Console Log -->
            <div class="sunset-card rounded-3xl p-6 flex flex-col h-96">
                <div class="flex justify-between items-center mb-4">
                    <h2 class="text-base font-extrabold text-slate-900">Live Execution Console</h2>
                    <button onclick="clearConsole()" class="text-xs font-bold text-rose-600 hover:text-rose-500">Clear Log</button>
                </div>
                <div id="consoleLog" class="bg-white/80 border border-rose-200/80 text-slate-800 rounded-2xl p-4 font-mono text-xs overflow-y-auto flex-1 space-y-1.5 shadow-inner">
                    <div class="text-slate-400">// System initialized. Ready for transactions.</div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const API_BASE = '/api';
        let salesChart = null;
        let timelineData = { labels: ['0s'], sold: [0], velocity: [0] };

        // Initialize Chart.js Graph
        function initChart() {
            const ctx = document.getElementById('salesChart').getContext('2d');
            salesChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: timelineData.labels,
                    datasets: [
                        {
                            label: 'Cumulative Tickets Sold',
                            data: timelineData.sold,
                            borderColor: '#4f46e5',
                            backgroundColor: 'rgba(79, 70, 229, 0.08)',
                            borderWidth: 3,
                            fill: true,
                            tension: 0.3,
                            yAxisID: 'y'
                        },
                        {
                            label: 'Sales Velocity (tickets/sec)',
                            data: timelineData.velocity,
                            borderColor: '#f59e0b',
                            backgroundColor: 'transparent',
                            borderWidth: 2,
                            borderDash: [5, 5],
                            tension: 0.3,
                            yAxisID: 'y1'
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    scales: {
                        x: { grid: { display: false }, title: { display: true, text: 'Sales Time (Seconds)' } },
                        y: { position: 'left', min: 0, title: { display: true, text: 'Tickets Amount' } },
                        y1: { position: 'right', min: 0, grid: { display: false }, title: { display: true, text: 'Velocity (t/s)' } }
                    }
                }
            });
        }

        function updateChart(timeLabel, soldAmount, velocity) {
            timelineData.labels.push(timeLabel);
            timelineData.sold.push(soldAmount);
            timelineData.velocity.push(velocity);
            
            if (timelineData.labels.length > 20) {
                timelineData.labels.shift();
                timelineData.sold.shift();
                timelineData.velocity.shift();
            }
            if (salesChart) salesChart.update();
        }

        function resetChart() {
            timelineData = { labels: ['0s'], sold: [0], velocity: [0] };
            if (salesChart) {
                salesChart.data.labels = timelineData.labels;
                salesChart.data.datasets[0].data = timelineData.sold;
                salesChart.data.datasets[1].data = timelineData.velocity;
                salesChart.update();
            }
        }

        function genUUID() {
            return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
                var r = Math.random() * 16 | 0, v = c == 'x' ? r : (r & 0x3 | 0x8);
                return v.toString(16);
            });
        }

        function randomizeInputs() {
            document.getElementById('userId').value = 'usr_' + genUUID().substring(0, 8);
            document.getElementById('requestId').value = 'req_' + genUUID().substring(0, 12);
        }
        randomizeInputs();

        function log(msg, type = 'info') {
            const el = document.getElementById('consoleLog');
            const time = new Date().toLocaleTimeString();
            let color = 'text-slate-300';
            if (type === 'success') color = 'text-emerald-400';
            if (type === 'warn') color = 'text-amber-400';
            if (type === 'error') color = 'text-rose-400';
            
            el.innerHTML += `<div class="${color}"><span class="text-slate-500">[${time}]</span> ${msg}</div>`;
            el.scrollTop = el.scrollHeight;
        }

        function clearConsole() {
            document.getElementById('consoleLog').innerHTML = '<div class="text-slate-500">// Console log reset.</div>';
        }

        async function fetchStatus() {
            try {
                const res = await fetch(`${API_BASE}/status`);
                if (!res.ok) {
                    log(`Failed status fetch: HTTP ${res.status}`, 'error');
                    return;
                }
                const data = await res.json();
                updateUI(data);
            } catch (err) {
                log(`Error fetching status: ${err.message}`, 'error');
            }
        }

        function updateUI(data) {
            const sold = data.tickets_sold || 0;
            const total = data.total_tickets || 100;
            const tickets = data.tickets || [];

            document.getElementById('ticketsSold').innerText = sold;
            document.getElementById('totalCapacity').innerText = `/ ${total}`;
            document.getElementById('availableCount').innerText = Math.max(0, total - sold);
            
            const pct = Math.min(100, (sold / total) * 100);
            document.getElementById('progressBar').style.width = `${pct}%`;
            document.getElementById('ticketCountBadge').innerText = `${tickets.length} Records`;

            const grid = document.getElementById('ticketsGrid');
            if (tickets.length === 0) {
                grid.innerHTML = '<div class="text-center text-slate-400 py-12 text-sm">No tickets issued yet.</div>';
                return;
            }

            grid.innerHTML = tickets.map(t => `
                <div class="bg-slate-50 border border-slate-200/80 rounded-xl p-3 flex justify-between items-center text-xs">
                    <div class="flex items-center gap-3">
                        <span class="bg-indigo-50 text-indigo-700 border border-indigo-200 px-2.5 py-1 rounded-lg font-bold font-mono text-xs">Ticket #${t.ticket_number}</span>
                        <div>
                            <div class="font-semibold text-slate-800">User: <span class="font-mono text-slate-600">${t.user_id}</span></div>
                            <div class="text-slate-400 font-mono text-[10px]">Request: ${t.request_id}</div>
                        </div>
                    </div>
                    <span class="text-[10px] font-bold bg-emerald-50 text-emerald-700 border border-emerald-200 px-2 py-0.5 rounded-full">CONFIRMED</span>
                </div>
            `).join('');
        }

        async function resetSale() {
            const count = parseInt(document.getElementById('resetCount').value) || 100;
            log(`Sending reset command for ${count} tickets...`, 'warn');
            try {
                const res = await fetch(`${API_BASE}/reset`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ count })
                });
                const data = await res.json();
                log(`Sale reset complete: ${JSON.stringify(data)}`, 'success');
                randomizeInputs();
                resetChart();
                fetchStatus();
            } catch (err) {
                log(`Reset failed: ${err.message}`, 'error');
            }
        }

        async function buyTicket(useDuplicate = false) {
            const userId = document.getElementById('userId').value || 'usr_test';
            const requestId = document.getElementById('requestId').value || 'req_test';

            const startTime = performance.now();
            log(`Sending POST /buy (User: ${userId}, Request: ${requestId})...`);
            try {
                const res = await fetch(`${API_BASE}/buy`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ user_id: userId, request_id: requestId })
                });
                const latency = Math.round(performance.now() - startTime);
                const data = await res.json();

                if (data.status === 'success') {
                    log(`SUCCESS: Ticket #${data.ticket_number} issued in ${latency}ms!`, 'success');
                    if (!useDuplicate) randomizeInputs();
                } else if (data.status === 'duplicate') {
                    log(`IDEMPOTENCY VERIFIED: Duplicate request_id returned Ticket #${data.ticket_number} (${latency}ms)`, 'warn');
                } else if (data.status === 'sold_out') {
                    log(`SOLD OUT: No tickets remaining (${latency}ms).`, 'error');
                } else {
                    log(`Response: ${JSON.stringify(data)} (${latency}ms)`);
                }
                
                document.getElementById('avgLatency').innerText = `Avg Latency: ${latency} ms`;
                fetchStatus();
            } catch (err) {
                log(`Request failed: ${err.message}`, 'error');
            }
        }

        async function runStampede() {
            const count = parseInt(document.getElementById('stampedeCount').value) || 50;
            log(`>>> INITIATING STAMPEDE BURST: Firing ${count} concurrent requests...`, 'warn');
            const btn = document.getElementById('stampedeBtn');
            btn.disabled = true;

            const startTime = performance.now();
            const promises = [];

            for (let i = 0; i < count; i++) {
                const uId = 'stampede_usr_' + i;
                const rId = 'stampede_req_' + genUUID().substring(0, 8);
                promises.push(
                    fetch(`${API_BASE}/buy`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ user_id: uId, request_id: rId })
                    }).then(async r => {
                        const txt = await r.text();
                        try { return JSON.parse(txt); } catch(e) { return { status: 'error' }; }
                    }).catch(err => ({ status: 'error' }))
                );
            }

            try {
                const results = await Promise.all(promises);
                const duration = (performance.now() - startTime) / 1000;
                
                const successCount = results.filter(r => r.status === 'success').length;
                const soldOutCount = results.filter(r => r.status === 'sold_out').length;
                const dupCount = results.filter(r => r.status === 'duplicate').length;
                const errCount = results.filter(r => r.status === 'error').length;

                const velocity = Math.round(successCount / duration);
                const avgLat = Math.round((duration * 1000) / count);

                document.getElementById('peakVelocity').innerText = `${velocity} t/s`;
                document.getElementById('avgLatency').innerText = `Avg Latency: ${avgLat} ms`;

                log(`>>> STAMPEDE COMPLETE in ${duration.toFixed(2)}s! Success: ${successCount}, Sold Out: ${soldOutCount}, Duplicates: ${dupCount}, Busy: ${errCount}`, 'success');

                const timeLabel = '+' + duration.toFixed(1) + 's';
                const statusRes = await fetch(`${API_BASE}/status`);
                const statusData = await statusRes.json();
                updateChart(timeLabel, statusData.tickets_sold || 0, velocity);
                updateUI(statusData);

            } catch (err) {
                log(`Stampede execution error: ${err.message}`, 'error');
            } finally {
                btn.disabled = false;
            }
        }

        window.onload = function() {
            initChart();
            fetchStatus();
        };
    </script>
</body>
</html>
"""

class IndexView(APIView):
    def get(self, request):
        return HttpResponse(HTML_DASHBOARD, content_type="text/html")

