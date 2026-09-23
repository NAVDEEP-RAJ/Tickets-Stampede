import argparse
import asyncio
import json
import random
import statistics
import sys
import time
import uuid
from collections import defaultdict
import os

import aiohttp

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

COLORS = {
    "GREEN": "\033[92m",
    "RED": "\033[91m",
    "RESET": "\033[0m",
    "YELLOW": "\033[93m",
    "BLUE": "\033[94m"
}

def p_pass(text):
    return f"{COLORS['GREEN']}{text}{COLORS['RESET']}"

def p_fail(text):
    return f"{COLORS['RED']}{text}{COLORS['RESET']}"

async def reset_sale(session, base_url, tickets):
    print(f"Resetting sale with {tickets} tickets...")
    try:
        async with session.post(f"{base_url}/reset", json={"count": tickets}) as resp:
            resp.raise_for_status()
            print(p_pass("Sale reset successfully."))
    except Exception as e:
        print(p_fail(f"Failed to reset sale: {e}"))
        sys.exit(1)

async def worker(session, base_url, req_data, start_event, results):
    await start_event.wait()
    user_id, request_id = req_data
    
    start_time = time.perf_counter()
    status_code = None
    response_body = None
    error = None

    try:
        async with session.post(
            f"{base_url}/buy",
            json={"user_id": user_id, "request_id": request_id}
        ) as resp:
            status_code = resp.status
            response_body = await resp.json()
    except Exception as e:
        error = str(e)
    
    latency = time.perf_counter() - start_time
    results.append({
        "user_id": user_id,
        "request_id": request_id,
        "status_code": status_code,
        "response_body": response_body,
        "error": error,
        "latency": latency
    })

async def main():
    parser = argparse.ArgumentParser(description="Ticket Stampede Load Testing Client")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api", help="Base URL")
    parser.add_argument("--tickets", type=int, default=100, help="Number of tickets to set up")
    parser.add_argument("--users", type=int, default=1000, help="Number of concurrent buy requests")
    parser.add_argument("--concurrency", type=int, default=200, help="Max concurrent connections")
    parser.add_argument("--duplicates", type=int, default=50, help="Number of duplicate request_ids")
    parser.add_argument("--output", default="buyer/results/run_results.json", help="Output file path")
    args = parser.parse_args()

    # Create directories for output if needed
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    connector = aiohttp.TCPConnector(limit=args.concurrency)
    async with aiohttp.ClientSession(connector=connector) as session:
        await reset_sale(session, args.url, args.tickets)

        print(f"Generating {args.users} user requests...")
        base_requests = [(str(uuid.uuid4()), str(uuid.uuid4())) for _ in range(args.users - args.duplicates)]
        
        duplicates = []
        if args.duplicates > 0 and base_requests:
            for _ in range(args.duplicates):
                # Pick a random request_id from base requests and use a new user_id
                target_req = random.choice(base_requests)
                duplicates.append((str(uuid.uuid4()), target_req[1]))
                
        all_requests = base_requests + duplicates
        random.shuffle(all_requests)

        print(f"Injected {len(duplicates)} duplicate requests.")
        print(f"Total requests to fire: {len(all_requests)}")

        start_event = asyncio.Event()
        results = []
        
        tasks = [
            asyncio.create_task(worker(session, args.url, req, start_event, results))
            for req in all_requests
        ]

        print("Firing requests...")
        test_start_time = time.perf_counter()
        start_event.set()
        await asyncio.gather(*tasks)
        test_duration = time.perf_counter() - test_start_time
        print("All requests completed.")

        print("Fetching final status...")
        try:
            async with session.get(f"{args.url}/status") as resp:
                resp.raise_for_status()
                final_status = await resp.json()
        except Exception as e:
            print(p_fail(f"Failed to fetch final status: {e}"))
            final_status = {}

    # Analysis
    successful = 0
    sold_out = 0
    duplicate_errors = 0
    other_errors = 0
    latencies = []

    for r in results:
        if r["latency"] is not None:
            latencies.append(r["latency"])
        
        if r["error"]:
            other_errors += 1
        elif r["status_code"] == 201:
            successful += 1
        elif r["status_code"] == 200:
            body_status = (r.get("response_body") or {}).get("status", "")
            if body_status == "sold_out":
                sold_out += 1
            elif body_status == "duplicate":
                duplicate_errors += 1
            else:
                other_errors += 1
        elif r["status_code"] == 409:
            other_errors += 1
        else:
            other_errors += 1

    rps = len(results) / test_duration if test_duration > 0 else 0
    latencies.sort()
    med_lat = statistics.median(latencies) if latencies else 0
    p95_lat = latencies[int(len(latencies) * 0.95)] if latencies else 0
    p99_lat = latencies[int(len(latencies) * 0.99)] if latencies else 0

    print("\n" + "="*50)
    print(f"{COLORS['BLUE']}METRICS SUMMARY{COLORS['RESET']}")
    print("="*50)
    print(f"Total Requests:      {len(results)}")
    print(f"Successful (201):    {successful}")
    print(f"Sold Out (200):      {sold_out}")
    print(f"Duplicates (200):    {duplicate_errors}")
    print(f"Errors:              {other_errors}")
    print(f"Duration:            {test_duration:.2f} s")
    print(f"Requests/sec:        {rps:.2f}")
    print(f"Latency Median:      {med_lat*1000:.2f} ms")
    print(f"Latency P95:         {p95_lat*1000:.2f} ms")
    print(f"Latency P99:         {p99_lat*1000:.2f} ms")

    print("\n" + "="*50)
    print(f"{COLORS['BLUE']}INVARIANT VERIFICATION{COLORS['RESET']}")
    print("="*50)
    
    total_tickets = final_status.get("total_tickets", args.tickets)
    tickets_sold = final_status.get("tickets_sold", 0)
    tickets_list = final_status.get("tickets", [])

    # 1. No overselling
    actual_count = len(tickets_list)
    inv1_pass = (tickets_sold <= total_tickets) and (actual_count == tickets_sold)
    inv1_msg = p_pass("PASS") if inv1_pass else p_fail("FAIL")
    print(f"1. No overselling: {inv1_msg} (Sold: {tickets_sold}, Limit: {total_tickets}, List length: {actual_count})")

    # 2. No duplicate ticket numbers
    ticket_numbers = [t.get("ticket_number") for t in tickets_list if t.get("ticket_number") is not None]
    unique_numbers = set(ticket_numbers)
    inv2_pass = len(ticket_numbers) == len(unique_numbers) and len(ticket_numbers) > 0
    inv2_msg = p_pass("PASS") if inv2_pass else p_fail("FAIL")
    print(f"2. No duplicate tickets: {inv2_msg} (Unique: {len(unique_numbers)} / {len(ticket_numbers)})")

    # 3. Idempotency
    # Group successful purchases by request_id
    success_reqs = defaultdict(list)
    for t in tickets_list:
        success_reqs[t.get("request_id")].append(t)
    
    inv3_pass = all(len(entries) == 1 for entries in success_reqs.values())
    inv3_msg = p_pass("PASS") if inv3_pass else p_fail("FAIL")
    print(f"3. Idempotency (no duplicate request_ids in db): {inv3_msg}")

    # 4. Consistency
    inv4_pass = tickets_sold == len(tickets_list)
    inv4_msg = p_pass("PASS") if inv4_pass else p_fail("FAIL")
    print(f"4. Consistency (tickets_sold matches list length): {inv4_msg}")

    output_data = {
        "metrics": {
            "total_requests": len(results),
            "successful": successful,
            "sold_out": sold_out,
            "duplicate_errors": duplicate_errors,
            "other_errors": other_errors,
            "duration_s": test_duration,
            "rps": rps,
            "latency_median_s": med_lat,
            "latency_p95_s": p95_lat,
            "latency_p99_s": p99_lat
        },
        "invariants": {
            "no_overselling": inv1_pass,
            "no_duplicate_tickets": inv2_pass,
            "idempotency": inv3_pass,
            "consistency": inv4_pass
        },
        "final_status": final_status,
        # "results": results # Omit full results if it's too large, or include if requested
    }

    try:
        with open(args.output, "w") as f:
            json.dump(output_data, f, indent=2)
        print(f"\nResults saved to {args.output}")
    except Exception as e:
        print(p_fail(f"Failed to save results: {e}"))

if __name__ == "__main__":
    asyncio.run(main())
