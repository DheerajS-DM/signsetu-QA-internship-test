# SignSetU QA Intern Final Round – Dheeraj Sutram

## Summary

**Test suite:** 16 automated `pytest` cases  
**Execution time:** ~60 seconds  
**Python version:** 3.11  
**Result:** 7 failed, 9 passed, 0 errors

| Test | Result |
|------|--------|
| `test_end_to_end_caption_lifecycle` | FAILED - Video processing never completed (timeout) |
| `test_ghost_data_after_deletion` | FAILED - Job stuck in pending, never transitions to completed |
| `test_idor_authorization_leak` | FAILED - User can DELETE another user's video (IDOR) |
| `test_concurrent_stress_handling` | PASSED |
| `test_pagination_data_handling` | FAILED - API accepts negative limit -1 |
| `test_malicious_payload_injection` | PASSED |
| `test_unsupported_method_crashing` | PASSED |
| `test_missing_mandatory_header_crash` | PASSED |
| `test_global_data_leak_on_index` | PASSED |
| `test_zombie_job_trigger` | PASSED |
| `test_schema_validation_failure` | FAILED - API accepts empty JSON payload, creates blank records |
| `test_out_of_order_execution` | FAILED - Captions returned before processing is triggered |
| `test_mass_deletion_vulnerability` | PASSED |
| `test_content_type_handling` | FAILED - API accepts raw text/garbage as valid video |
| `test_concurrent_stress_handling_500` | PASSED |
| `test_mandatory_header_enforcement_on_data_endpoints` | PASSED |

---

## Detailed Bug Explanations

### 1. Video processing never completes - stuck in pending
- **Test:** `test_end_to_end_caption_lifecycle` / `test_ghost_data_after_deletion`
- **How found:** After triggering `POST /api/videos/{id}/process-captions`, the test polls `GET /api/videos/{id}` every 2 seconds for up to 40 seconds. The status remains `"pending"` indefinitely, never reaching `"completed"` or `"failed"`.
- **Severity:** CRITICAL - The core async workflow is broken. No captions are ever generated.

### 2. IDOR - cross-user deletion
- **Test:** `test_idor_authorization_leak`
- **How found:**  
  1. Candidate A (authenticated with `X-Candidate-ID: Dheeraj...`) creates a video.  
  2. Candidate B (completely different `X-Candidate-ID`) authenticates separately.  
  3. Candidate B attempts to `DELETE /api/videos/{id}` of Candidate A's video.  
  4. The API returns `200 OK`, proving that authorization is missing.
- **Severity:** CRITICAL - Any user can delete any other user's data. Complete isolation failure.

### 3. Empty payload creates blank video records
- **Test:** `test_schema_validation_failure`
- **How found:** `POST /api/videos` with `json={}` (empty object) returns `201 Created` with a new `id`. The created record has no title, no URL - completely blank.
- **Severity:** CRITICAL - No schema validation. Allows garbage data to pollute the database.

### 4. Captions available without processing
- **Test:** `test_out_of_order_execution`
- **How found:**  
  1. Create a video but **never** call `process-captions`.  
  2. Immediately `GET /api/captions?videoId={id}` returns `200 OK` with a (probably empty or junk) caption list.  
  3. A correct API should return `404` or `400` because no captions exist.
- **Severity:** HIGH - State machine violation. Clients can fetch captions before they are generated.

### 5. API accepts raw text/garbage as a video
- **Test:** `test_content_type_handling`
- **How found:** Send `POST /api/videos` with `Content-Type: text/plain` and a plain string payload (not JSON). The API returns `200 OK` and creates a video record.
- **Severity:** MEDIUM - Incorrect content-type handling; may lead to parsing errors or injection.

### 6. Negative limit accepted in pagination
- **Test:** `test_pagination_data_handling`
- **How found:** `GET /api/videos?limit=-1` returns `200 OK` with a list of videos. A secure API should reject negative values (`400 Bad Request`).
- **Severity:** MEDIUM - Bad input validation; could cause unexpected database behaviour or DoS.

### 7. Additional vulnerability - No rate limiting
- **Test:** `test_concurrent_stress_handling_500`
- **Observation:** 500 concurrent requests all returned `200 OK`. No `429` rate-limit responses. This is not a hard failure but a performance vulnerability for production.

---

## Test Architecture & Workflow

### Overall Structure
- **`conftest.py`**  
  - `api_session` fixture: creates a `requests.Session`, adds mandatory `X-Candidate-ID` header, authenticates once (`POST /api/auth`), and attaches the `Bearer` token.  
  - `pytest_terminal_summary`: custom hook that prints a clean vulnerability summary after test run.

- **`test_pipeline.py`**  
  - Contains **16 test functions**, each targeting a specific behaviour or vulnerability.  
  - Shared helper: `poll_until_complete(api_session, video_id, max_retries=20, interval=2)` - polls video status, handles 401 token refresh, returns `"completed"`, `"failed"`, `"timeout"`, or `"api_error"`.

### Workflow of Key Tests

#### E2E Lifecycle (`test_end_to_end_caption_lifecycle`)
1. Create video -> obtain `video_id`.  
2. Trigger caption processing.  
3. Poll with `poll_until_complete` (up to 40 seconds).  
4. Assert `result == "completed"`.  
5. Fetch captions and assert `200 OK`.  
6. Finally, delete video.

#### IDOR / Authorization (`test_idor_authorization_leak`)
- Uses **two independent sessions** (different `X-Candidate-ID`).  
- Candidate A creates a video.  
- Candidate B authenticates and attempts `GET` and `DELETE` on A's video ID.  
- Failure (i.e., successful deletion by B) is reported as a critical bug.

#### Concurrency & Load
- `test_concurrent_stress_handling`: 50 threads hammering `GET /api/videos/{id}`. Checks for `500/502` crashes and rate-limiting (`429`).  
- `test_concurrent_stress_handling_500`: same but with 500 threads (stress test). Warns if no rate limiting is present.

#### Input Validation
- **Pagination:** sends `limit=999999999`, `limit=-1`, `limit=invalid_string` and inspects status codes.  
- **Schema validation:** sends empty `{}` as create payload.  
- **Content-type:** sends `text/plain` with garbage string.

#### State Machine Violations
- **Out-of-order:** fetches captions without ever calling `process-captions`.  
- **Zombie job:** deletes video, then tries to trigger processing on the deleted ID.

#### Security (additional)
- `test_global_data_leak_on_index`: checks if `GET /api/videos` leaks other users' videos.  
- `test_missing_mandatory_header_crash`: omits `X-Candidate-ID` header on auth endpoint.  
- `test_mandatory_header_enforcement_on_data_endpoints`: omits header on data endpoint.  
- `test_mass_deletion_vulnerability`: attempts `DELETE /api/videos` (root collection).  
- `test_unsupported_method_crashing`: sends `PUT` to `/api/videos`.  
- `test_malicious_payload_injection`: injects NoSQL/SQL operators into title/url.

---

## Potential Improvements

Although the test suite successfully uncovered 7 bugs, the following enhancements would make it more robust and production-ready.

### 1. Polling with Exponential Backoff
**Current:** Fixed 2-second intervals, 20 retries -> 40 seconds timeout.  
**Improvement:**  
```python
interval = min(initial_backoff * (2 ** attempt), max_backoff)