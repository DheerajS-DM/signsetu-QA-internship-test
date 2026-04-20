import pytest
import requests

BASE_URL = "https://qa-testing-navy.vercel.app"
CANDIDATE_ID = "Dheeraj-Sutram-QA-Intern-Test" # Mandatory header

@pytest.fixture(scope="session")
def api_session():
    """Handles authentication and provides a session with default headers."""
    session = requests.Session()
    session.headers.update({"X-Candidate-ID": CANDIDATE_ID})
    
    # Authenticate to get session token
    response = session.post(f"{BASE_URL}/api/auth")
    token = response.json().get("token")
    
    # Update session with the auth token for subsequent calls
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session
# Add to the bottom of conftest.py

def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Custom hook to format the final output cleanly."""
    terminalreporter.section("SIGNSETU VULNERABILITY SUMMARY", bold=True, yellow=True)
    
    failures = terminalreporter.stats.get("failed", [])
    if not failures:
        terminalreporter.write_line("No vulnerabilities found. The system is secure.")
        return

    for report in failures:
        # Extract just the function name and the custom error message
        func_name = report.nodeid.split("::")[-1]
        error_msg = report.longrepr.reprcrash.message if hasattr(report.longrepr, 'reprcrash') else str(report.longrepr)
        
        # Strip the "Failed: " prefix if it exists for cleaner reading
        clean_error = error_msg.replace("Failed: ", "")
        
        terminalreporter.write_line(f"TARGET   : {func_name}", bold=True, red=True)
        terminalreporter.write_line(f"BREACH   : {clean_error}\n")
