"""Quick smoke test for the IMAPsync Web app."""
import base64
import os
import sys

os.environ["FLASK_ENV"] = "development"
os.environ["DEBUG"] = "0"

# Add project root to path
project = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project)

from app import create_app

app = create_app()

with app.test_client() as client:
    # Auth required
    resp = client.get("/")
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"
    print("OK: Auth required for /")

    resp = client.get("/api/stats")
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"
    print("OK: Auth required for /api/stats")

    # Valid auth
    from models.job import get_setting
    username = get_setting("auth_username") or "admin"
    pw = app.config.get("AUTH_PASSWORD", "admin")
    auth = "Basic " + base64.b64encode(f"{username}:{pw}".encode()).decode()

    for path, label in [
        ("/", "Dashboard"),
        ("/accounts", "Accounts"),
        ("/jobs", "Jobs"),
        ("/settings", "Settings"),
        ("/accounts/add", "Add Account"),
        ("/jobs/create", "Create Job"),
    ]:
        resp = client.get(path, headers={"Authorization": auth})
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code} for {path}"
        print(f"OK: {label} ({resp.status_code})")

    resp = client.get("/api/stats", headers={"Authorization": auth})
    data = resp.get_json()
    assert "running" in data
    assert "max_concurrent" in data
    print(f"OK: API stats -> {data}")

print("\n*** ALL SMOKE TESTS PASSED ***")
