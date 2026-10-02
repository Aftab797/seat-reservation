import sys
import uuid
import random
import requests
import json
import time
import asyncio
import aiohttp
from collections import Counter

async def make_request(session, base_url, show_id, user_id, idempotency_key, seats):
    headers = {
        "Authorization": f"Bearer {user_id}",
        "Idempotency-Key": idempotency_key,
        "Content-Type": "application/json"
    }
    payload = {"seats": seats}
    url = f"{base_url}/shows/{show_id}/reserve"
    
    start_time = time.time()
    try:
        async with session.post(url, json=payload, headers=headers) as response:
            status = response.status
            body = await response.json()
            return status, body, time.time() - start_time
    except Exception as e:
        return 500, {"error": str(e)}, time.time() - start_time

async def worker(session, base_url, show_id, tasks_queue, results):
    while True:
        task = await tasks_queue.get()
        if task is None:
            break
        
        user_id, idempotency_key, seats = task
        status, body, duration = await make_request(session, base_url, show_id, user_id, idempotency_key, seats)
        
        reason = body.get("code") if isinstance(body, dict) else "unknown"
        results.append((status, reason))
        
        tasks_queue.task_done()

async def run_burst(base_url, total_requests, concurrency, show_id):
    results = []
    tasks_queue = asyncio.Queue()
    
    hot_seats = ["A1", "A2", "A3", "B1", "B2"]
    other_seats = [f"{chr(65+i)}{j}" for i in range(5) for j in range(4, 21)]
    all_seats = hot_seats + other_seats
    
    users = [f"user-{i}" for i in range(100)]
    
    # Pre-generate tasks
    for i in range(total_requests):
        user_id = random.choice(users)
        
        # 10% chance of idempotency replay
        if random.random() < 0.1 and i > 0:
            idempotency_key = f"key-replay-{random.randint(0, i-1)}"
        else:
            idempotency_key = str(uuid.uuid4())
            
        # 50% chance of targeting hot seats
        if random.random() < 0.5:
            num_seats = random.randint(1, 4)
            seats = random.sample(hot_seats, min(num_seats, len(hot_seats)))
        else:
            num_seats = random.randint(1, 4)
            seats = random.sample(all_seats, num_seats)
            
        await tasks_queue.put((user_id, idempotency_key, seats))
        
    async with aiohttp.ClientSession() as session:
        workers = [asyncio.create_task(worker(session, base_url, show_id, tasks_queue, results))
                  for _ in range(concurrency)]
        
        await tasks_queue.join()
        
        for _ in workers:
            await tasks_queue.put(None)
        await asyncio.gather(*workers)
        
    return results

def setup_show(base_url):
    print("Setting up test show...")
    headers = {"Authorization": "Bearer admin"}
    seats = [f"{chr(65+i)}{j}" for i in range(5) for j in range(1, 21)] # 100 seats
    payload = {
        "name": "Load Test Show",
        "price_paise": 10000,
        "per_user_limit": 4,
        "seats": seats
    }
    
    try:
        response = requests.post(f"{base_url}/shows", json=payload, headers=headers)
        response.raise_for_status()
        show = response.json()
        print(f"Created show {show['id']} with {show['total_seats']} seats")
        return show['id']
    except requests.exceptions.RequestException as e:
        print(f"Failed to create show: {e}")
        if e.response is not None:
             print(e.response.text)
        sys.exit(1)

def print_results(results, base_url, show_id):
    print("\n" + "="*40)
    print("BURST RESULT")
    print("="*40)
    
    status_counts = Counter(r[0] for r in results)
    reason_counts = Counter(f"{r[0]} {r[1]}" for r in results)
    
    print(f"Total requests:       {len(results):,}")
    print(f"201 confirmed:             {status_counts.get(201, 0)}")
    
    for reason, count in sorted(reason_counts.items()):
        if reason.startswith("409"):
            print(f"{reason:<24} {count}")
            
    print(f"5xx:                        {sum(v for k, v in status_counts.items() if k >= 500)}")
    
    print("\nFinal seats")
    print("-" * 11)
    
    try:
        response = requests.get(f"{base_url}/shows/{show_id}")
        response.raise_for_status()
        show = response.json()
        print(f"total:                   {show['total_seats']}")
        print(f"available:                {show['available']}")
        print(f"confirmed:                 {show['confirmed']}")
        
        print("\nInvariant")
        print("-" * 9)
        invariant_str = f"{show['available']} + {show['confirmed']} = {show['total_seats']}"
        print(invariant_str)
        if show['available'] + show['confirmed'] == show['total_seats']:
            print("PASS")
        else:
            print("FAIL")
            
    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch final show state: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python burst.py <base_url> [total_requests] [concurrency]")
        sys.exit(1)
        
    base_url = sys.argv[1]
    total_requests = int(sys.argv[2]) if len(sys.argv) > 2 else 20000
    concurrency = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    
    show_id = setup_show(base_url)
    
    print(f"\nStarting burst against {base_url}")
    print(f"Requests: {total_requests}, Concurrency: {concurrency}")
    
    results = asyncio.run(run_burst(base_url, total_requests, concurrency, show_id))
    
    print_results(results, base_url, show_id)
    print("="*40)

