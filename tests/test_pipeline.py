import pytest
import time
import requests

BASE_URL = "https://qa-testing-navy.vercel.app"

def poll_until_complete(api_session, video_id, max_retries=20, interval=2):
    """
    State-aware polling that handles temporal constraints, cold starts,
    and aggressive token expiration (401 re-auth).
    """
    for attempt in range(max_retries):
        resp = api_session.get(f"{BASE_URL}/api/videos/{video_id}")
        
        # Handle 401 Session Drop (The Re-auth Logic)
        if resp.status_code == 401:
            # Re-authenticate to get a fresh token
            auth_resp = api_session.post(f"{BASE_URL}/api/auth")
            new_token = auth_resp.json().get("token")
            if new_token:
                api_session.headers.update({"Authorization": f"Bearer {new_token}"})
            continue # Skip the rest of the loop and retry the poll

        # Catch other actual API crashes
        if resp.status_code != 200:
            return "api_error"
        
        status = resp.json().get("status")
        if status == "completed":
            return "completed"
        if status in ["failed", "error"]:
            return "failed"
        
        time.sleep(interval)
        
    return "timeout"

def test_end_to_end_caption_lifecycle(api_session):
    # 1. Create
    create_resp = api_session.post(f"{BASE_URL}/api/videos", json={"title": "E2E Test"})
    video_id = create_resp.json().get("id")

    try:
        # 2. Trigger
        api_session.post(f"{BASE_URL}/api/videos/{video_id}/process-captions")

        # 3. Poll (Using helper with timeout)
        result = poll_until_complete(api_session, video_id)
        assert result == "completed", f"API BUG: Video processing never completed. Last status: {result}"

        # 4. Verify
        caption_resp = api_session.get(f"{BASE_URL}/api/captions?videoId={video_id}")
        assert caption_resp.status_code == 200
        
    finally:
        # 5. Teardown
        api_session.delete(f"{BASE_URL}/api/videos/{video_id}")
def test_ghost_data_after_deletion(api_session):
    """
    Verifies if deleting a video properly cleans up associated data, 
    accounting for eventual consistency in distributed databases.
    """
    # Setup
    create_resp = api_session.post(f"{BASE_URL}/api/videos", json={"title": "Cleanup Test"})
    video_id = create_resp.json().get("id")
    
    api_session.post(f"{BASE_URL}/api/videos/{video_id}/process-captions")
    
    # Use the hardened poller
    result = poll_until_complete(api_session, video_id)
    if result == "timeout":
        pytest.fail("BUG FOUND: API status 'pending' never transitions to 'completed'. Job is stuck.")

    # Execute Delete
    api_session.delete(f"{BASE_URL}/api/videos/{video_id}")

    # Verify Captions are gone (with eventual consistency buffer)
    max_checks = 5
    for _ in range(max_checks):
        get_captions_resp = api_session.get(f"{BASE_URL}/api/captions?videoId={video_id}")
        
        # If it returns 404 or 400, the data is successfully gone.
        if get_captions_resp.status_code != 200:
            return # Exit the test successfully
            
        time.sleep(2) # Buffer for background workers to purge data
        
    # If the loop finishes and we are still getting 200 OKs, the bug is real.
    pytest.fail("BUG FOUND: Caption data persists after parent video is deleted (Eventual Consistency timeout exceeded).")

def test_idor_authorization_leak(api_session):
    """
    Tests if a user can access another user's data by manipulating the X-Candidate-ID.
    """
    import requests
    
    # 1. Create a video with your main session (Candidate A)
    create_resp = api_session.post(f"{BASE_URL}/api/videos", json={"title": "IDOR Test Video"})
    video_id = create_resp.json().get("id")

    try:
        # 2. Create a brand new session (Candidate B)
        rogue_session = requests.Session()
        rogue_session.headers.update({"X-Candidate-ID": "COMPLETELY_DIFFERENT_ID_999"})
        
        # Authenticate Rogue Session
        auth_resp = rogue_session.post(f"{BASE_URL}/api/auth")
        rogue_token = auth_resp.json().get("token")
        rogue_session.headers.update({"Authorization": f"Bearer {rogue_token}"})

        # 3. Attempt to fetch Candidate A's video using Candidate B's session
        rogue_get_resp = rogue_session.get(f"{BASE_URL}/api/videos/{video_id}")
        
        if rogue_get_resp.status_code == 200:
            pytest.fail("CRITICAL BUG FOUND: IDOR/BOLA. A user can access records belonging to another candidate's ID.")
            
        # 4. Attempt to DELETE Candidate A's video using Candidate B's session
        rogue_delete_resp = rogue_session.delete(f"{BASE_URL}/api/videos/{video_id}")
        
        if rogue_delete_resp.status_code in [200, 204]:
            pytest.fail("CRITICAL BUG FOUND: IDOR/BOLA. A user can DELETE records belonging to another candidate's ID.")

    finally:
        # Teardown with the authorized session
        api_session.delete(f"{BASE_URL}/api/videos/{video_id}")

import concurrent.futures

def test_concurrent_stress_handling(api_session):
    """
    Blasts the API with concurrent requests to check for rate limiting and server stability.
    """
    # Create a dummy video to hammer
    create_resp = api_session.post(f"{BASE_URL}/api/videos", json={"title": "Stress Test"})
    video_id = create_resp.json().get("id")

    def ping_status():
        return api_session.get(f"{BASE_URL}/api/videos/{video_id}").status_code

    # Launch 50 concurrent requests
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        results = list(executor.map(lambda _: ping_status(), range(50)))

    # Clean up immediately
    api_session.delete(f"{BASE_URL}/api/videos/{video_id}")

    # Analyze results
    if 500 in results or 502 in results:
        pytest.fail("BUG FOUND: Concurrent requests cause the server to crash (500/502). No rate limiting detected.")
    
    if 429 not in results and all(code == 200 for code in results):
        # This isn't necessarily a hard fail, but it's a huge red flag for a production API.
        print("\nWARNING: API absorbed 50 concurrent requests with no 429 Rate Limit. Potential DDoS vulnerability.")

def test_pagination_data_handling(api_session):
    """
    Tests how the API handles extreme, negative, or malformed data bounds.
    """
    # Test 1: Massive Limit (Testing for memory bloat / DDoS vector)
    resp_massive = api_session.get(f"{BASE_URL}/api/videos?limit=999999999")
    if resp_massive.status_code == 500:
        pytest.fail("BUG FOUND: API throws unhandled 500 error on massive limit (Performance/Memory leak).")
    
    # Test 2: Negative Limit (Testing for bad database query handling)
    resp_negative = api_session.get(f"{BASE_URL}/api/videos?limit=-1")
    if resp_negative.status_code == 200:
        pytest.fail("BUG FOUND: API accepts negative limits, potentially breaking pagination logic.")
    elif resp_negative.status_code == 500:
         pytest.fail("BUG FOUND: API crashes (500) on negative limit instead of returning 400 Bad Request.")
         
    # Test 3: Type confusion
    resp_string = api_session.get(f"{BASE_URL}/api/videos?limit=invalid_string")
    if resp_string.status_code == 500:
         pytest.fail("BUG FOUND: API fails to type-check limit parameter, resulting in a server crash.")
# ==========================================
# BONUS BUG 1: Payload & NoSQL/SQL Injection
# ==========================================
def test_malicious_payload_injection(api_session):
    """
    Injects database query operators and SQL syntax into standard string fields.
    """
    # Attempting to pass a NoSQL evaluation object and SQL termination string
    malicious_payload = {
        "title": {"$gt": ""}, 
        "url": "'; DROP TABLE videos; --"
    }
    
    resp = api_session.post(f"{BASE_URL}/api/videos", json=malicious_payload)
    
    if resp.status_code == 500:
        pytest.fail("BONUS BUG CAUGHT: API crashes (500) on injected payload characters. High probability of SQL/NoSQL injection vulnerability.")
    elif resp.status_code in [200, 201]:
        # If it returns 200, check if it blindly saved the dictionary object as a string
        if isinstance(resp.json().get("title"), dict):
            pytest.fail("BONUS BUG CAUGHT: API blindly accepts and processes dictionary objects in string fields (NoSQL Injection Vector).")

# ==========================================
# BONUS BUG 2: Unhandled HTTP Methods
# ==========================================
def test_unsupported_method_crashing(api_session):
    """
    Tests if the API throws a polite 405 (Method Not Allowed) or completely crashes 
    when hit with unsupported HTTP methods.
    """
    # The videos endpoint expects GET or POST. We will hit it with a PUT.
    resp = api_session.put(f"{BASE_URL}/api/videos")
    
    if resp.status_code == 500:
        pytest.fail("BONUS BUG CAUGHT: API throws a 500 Internal Server Error on unsupported PUT method instead of a 405 Method Not Allowed. Poor error handling.")

# ==========================================
# BONUS BUG 3: Mandatory Header Validation
# ==========================================
def test_missing_mandatory_header_crash():
    """
    The instructions explicitly state X-Candidate-ID is mandatory.
    This tests if the API fails securely or crashes when it is completely omitted.
    """
    import requests
    rogue_session = requests.Session() # A completely blank session with no headers
    
    resp = rogue_session.post(f"{BASE_URL}/api/auth")
    
    if resp.status_code == 500:
        pytest.fail("BONUS BUG CAUGHT: API crashes (500) when the mandatory X-Candidate-ID header is missing, instead of returning a 400/401.")
    elif resp.status_code == 200:
        pytest.fail("BONUS BUG CAUGHT: API successfully authenticates even when the mandatory X-Candidate-ID header is completely missing.")
# ==========================================
# CRITICAL BUG 5: Global Data Leak (Cross-Tenant Indexing)
# ==========================================
def test_global_data_leak_on_index(api_session):
    """
    Checks if the GET /api/videos endpoint scopes results to the X-Candidate-ID
    or if it leaks the entire database of all candidates.
    """
    import requests
    
    # 1. Create a video with Candidate A (Your main session)
    create_resp = api_session.post(f"{BASE_URL}/api/videos", json={"title": "Data Leak Test"})
    video_id = create_resp.json().get("id")
    
    try:
        # 2. Authenticate as a completely different user (Candidate B)
        rogue_session = requests.Session()
        rogue_session.headers.update({"X-Candidate-ID": "ISOLATION_TEST_ID_007"})
        auth_resp = rogue_session.post(f"{BASE_URL}/api/auth")
        rogue_token = auth_resp.json().get("token")
        if rogue_token:
            rogue_session.headers.update({"Authorization": f"Bearer {rogue_token}"})
        
        # 3. Candidate B calls the index endpoint
        list_resp = rogue_session.get(f"{BASE_URL}/api/videos")
        assert list_resp.status_code == 200, "Index endpoint failed."
        
        all_videos = list_resp.json()
        
        # 4. Check if Candidate A's video is visible in Candidate B's list
        if isinstance(all_videos, list):
            if any(vid.get("id") == video_id for vid in all_videos):
                pytest.fail("CRITICAL BUG FOUND: GET /api/videos leaks data globally. It is not scoped to X-Candidate-ID.")
    
    finally:
        # Teardown using the authorized session
        api_session.delete(f"{BASE_URL}/api/videos/{video_id}")
# ==========================================
# BONUS BUG 4: State Machine Re-entry (Zombie Jobs)
# ==========================================
def test_zombie_job_trigger(api_session):
    """
    Tests if the backend allows triggering jobs on deleted or non-existent records.
    """
    # 1. Create and immediately delete a video
    create_resp = api_session.post(f"{BASE_URL}/api/videos", json={"title": "Zombie Test"})
    video_id = create_resp.json().get("id")
    api_session.delete(f"{BASE_URL}/api/videos/{video_id}")
    
    # 2. Trigger processing on the now-deleted ID
    trigger_resp = api_session.post(f"{BASE_URL}/api/videos/{video_id}/process-captions")
    
    if trigger_resp.status_code in [200, 202]:
        pytest.fail("BONUS BUG CAUGHT: API accepts caption processing jobs for deleted/non-existent video IDs (Zombie Jobs).")
# ==========================================
# BONUS BUG 5: Schema Validation Failure
# ==========================================
def test_schema_validation_failure(api_session):
    """
    Tests if the API accepts completely empty schemas or crashes when required fields are missing.
    """
    # Send an empty JSON object
    resp = api_session.post(f"{BASE_URL}/api/videos", json={})
    
    if resp.status_code in [200, 201]:
        # Clean up if it actually created it
        video_id = resp.json().get("id")
        if video_id:
            api_session.delete(f"{BASE_URL}/api/videos/{video_id}")
        pytest.fail("BONUS BUG CAUGHT: API has zero schema validation. It accepts completely empty payloads and creates blank records.")
    elif resp.status_code == 500:
        pytest.fail("BONUS BUG CAUGHT: API crashes (500) on empty payloads instead of validating and returning a 400 Bad Request.")
# ==========================================
# REDUNDANCY 1: State Machine Violation (Out of Order)
# ==========================================
def test_out_of_order_execution(api_session):
    """
    Attempts to fetch captions for a video that was created but NEVER triggered for processing.
    """
    create_resp = api_session.post(f"{BASE_URL}/api/videos", json={"title": "Out of Order Test"})
    video_id = create_resp.json().get("id")

    try:
        # We skip the trigger step and immediately ask for captions
        caption_resp = api_session.get(f"{BASE_URL}/api/captions?videoId={video_id}")
        
        # A good API should return 404 (Not Found) or 400 (Bad Request).
        if caption_resp.status_code == 200:
            pytest.fail("BONUS BUG CAUGHT: API returns 200 OK for captions on a video that was never processed.")
        elif caption_resp.status_code == 500:
            pytest.fail("BONUS BUG CAUGHT: API crashes (500) when requesting captions for unprocessed videos.")
    finally:
        api_session.delete(f"{BASE_URL}/api/videos/{video_id}")
# ==========================================
# REDUNDANCY 2: Mass Deletion / Truncation Attempt
# ==========================================
def test_mass_deletion_vulnerability(api_session):
    """
    Attempts to call DELETE on the root collection instead of a specific ID.
    """
    # A resilient REST API should block this with a 405 Method Not Allowed.
    resp = api_session.delete(f"{BASE_URL}/api/videos")
    
    if resp.status_code in [200, 204]:
        pytest.fail("CRITICAL BUG FOUND: API allows DELETE requests on the root /api/videos endpoint, potentially wiping the entire database.")
    elif resp.status_code == 500:
         pytest.fail("BONUS BUG CAUGHT: API crashes (500) on root DELETE request instead of safely rejecting it with a 405.")
# ==========================================
# REDUNDANCY 3: Content-Type & Garbage Handling
# ==========================================
def test_content_type_handling(api_session):
    """
    Tests how the API handles requests with incorrect Content-Types or malformed body data.
    """
    # Sending plain text instead of JSON
    headers = {"Content-Type": "text/plain"}
    payload = "This is definitely not valid JSON data."
    
    # We must use api_session.request to override the default JSON formatting
    resp = api_session.post(f"{BASE_URL}/api/videos", data=payload, headers=headers)
    
    if resp.status_code == 500:
        pytest.fail("BONUS BUG CAUGHT: API crashes (500) when receiving non-JSON content types.")
    elif resp.status_code in [200, 201]:
        pytest.fail("BONUS BUG CAUGHT: API accepts raw text/garbage payloads and processes them with a 200 OK.")
# ==========================================
# REDUNDANCY: High-Concurrency Stress Test (500 Hits)
# ==========================================
def test_concurrent_stress_handling_500(api_session):
    """
    Blasts the API with 500 concurrent requests to identify 
    rate-limiting thresholds and server stability bounds.
    
    STRESS TEST: 500 concurrent requests.
    
    Note: This uses threading (not asyncio) and may have client-side overhead.
    A production load test would use Locust/k6. However, even with this
    naive implementation, the server showed instability (5xx errors) at high load,
    indicating a need for rate limiting or better connection handling.

    
    """
    import concurrent.futures

    # 1. Create a dummy video to hammer
    create_resp = api_session.post(f"{BASE_URL}/api/videos", json={"title": "Load Test"})
    video_id = create_resp.json().get("id")

    def ping_status():
        # Using a fresh session for each "user" would be more realistic,
        # but for a raw stress test, hammering with one session is sufficient.
        return api_session.get(f"{BASE_URL}/api/videos/{video_id}").status_code

    # 2. Launch 500 concurrent requests (Pushing the limits of ThreadPool)
    # Note: On some systems, 500 threads might reach the OS limit.
    with concurrent.futures.ThreadPoolExecutor(max_workers=500) as executor:
        results = list(executor.map(lambda _: ping_status(), range(500)))

    # 3. Clean up immediately
    api_session.delete(f"{BASE_URL}/api/videos/{video_id}")

    # 4. Analyze Results for Bug Reports
    server_crashes = [code for code in results if code in [500, 502, 503, 504]]
    rate_limited = [code for code in results if code == 429]
    successes = [code for code in results if code == 200]

    # VULNERABILITY: No Rate Limiting
    if not rate_limited and len(successes) == 500:
        # While the test passes for the server, this is a "vulnerability" in a real API
        print("\n[WARNING] ZERO RATE LIMITING: API absorbed 500 concurrent requests with zero 429 errors.")

    # VULNERABILITY: Server Instability
    if len(server_crashes) > 0:
        pytest.fail(f"BONUS BUG CAUGHT: High load (500 hits) caused {len(server_crashes)} server-side crashes (5xx errors).")

    # If it hit 429s and no 500s, the system is robust!
    assert len(server_crashes) == 0

def test_mandatory_header_enforcement_on_data_endpoints():
    """Ensures X-Candidate-ID is required for data access. Addresses Flaw #5."""
    import requests
    bad_session = requests.Session() # No headers
    resp = bad_session.get(f"{BASE_URL}/api/videos")
    assert resp.status_code in [400, 401], "Security Flaw: API allows data access without Candidate ID."