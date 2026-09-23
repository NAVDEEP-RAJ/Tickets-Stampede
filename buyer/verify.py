import argparse
import requests
import sys
from collections import defaultdict

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

def main():
    parser = argparse.ArgumentParser(description="Ticket Stampede Invariant Verifier")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api", help="Base URL")
    parser.add_argument("--expected-tickets", type=int, default=100, dest="expected_tickets", help="Expected max tickets")
    args = parser.parse_args()

    print(f"Fetching final status from {args.url}/status ...")
    try:
        response = requests.get(f"{args.url}/status", timeout=10)
        response.raise_for_status()
        final_status = response.json()
    except Exception as e:
        print(p_fail(f"Failed to fetch status: {e}"))
        sys.exit(1)

    print("\n" + "="*50)
    print(f"{COLORS['BLUE']}INVARIANT VERIFICATION{COLORS['RESET']}")
    print("="*50)

    total_tickets = final_status.get("total_tickets", args.expected_tickets)
    tickets_sold = final_status.get("tickets_sold", 0)
    tickets_list = final_status.get("tickets", [])

    all_pass = True

    # 1. No overselling
    actual_count = len(tickets_list)
    inv1_pass = (tickets_sold <= total_tickets) and (actual_count == tickets_sold)
    inv1_msg = p_pass("PASS") if inv1_pass else p_fail("FAIL")
    print(f"1. No overselling: {inv1_msg} (Sold: {tickets_sold}, Limit: {total_tickets}, List length: {actual_count})")
    if not inv1_pass: all_pass = False

    # 2. No duplicate ticket numbers
    ticket_numbers = [t.get("ticket_number") for t in tickets_list if t.get("ticket_number") is not None]
    unique_numbers = set(ticket_numbers)
    inv2_pass = len(ticket_numbers) == len(unique_numbers) and (len(ticket_numbers) > 0 or total_tickets == 0 or tickets_sold == 0)
    inv2_msg = p_pass("PASS") if inv2_pass else p_fail("FAIL")
    print(f"2. No duplicate tickets: {inv2_msg} (Unique: {len(unique_numbers)} / {len(ticket_numbers)})")
    if not inv2_pass: all_pass = False

    # 3. Idempotency
    success_reqs = defaultdict(list)
    for t in tickets_list:
        success_reqs[t.get("request_id")].append(t)
    
    inv3_pass = all(len(entries) == 1 for entries in success_reqs.values())
    inv3_msg = p_pass("PASS") if inv3_pass else p_fail("FAIL")
    print(f"3. Idempotency (no duplicate request_ids in db): {inv3_msg}")
    if not inv3_pass: all_pass = False

    # 4. Consistency
    inv4_pass = tickets_sold == len(tickets_list)
    inv4_msg = p_pass("PASS") if inv4_pass else p_fail("FAIL")
    print(f"4. Consistency (tickets_sold matches list length): {inv4_msg}")
    if not inv4_pass: all_pass = False

    print("\n" + "="*50)
    if all_pass:
        print(p_pass("ALL INVARIANTS PASSED!"))
        sys.exit(0)
    else:
        print(p_fail("SOME INVARIANTS FAILED!"))
        sys.exit(1)

if __name__ == "__main__":
    main()
