# tests/root/test_root.py
import pytest

@pytest.mark.root
def test_root_up(client):
    resp = client.get("/")
    
    # Custom failure messages help you debug without opening a debugger
    assert resp.status_code == 200, f"Expected 200 but got {resp.status_code}. Response: {resp.text}"
    
    content_type = resp.headers.get("content-type", "")
    assert content_type.startswith("application/json"), f"Expected JSON, got {content_type}"
    
    body = resp.json()
    
    # This will show up if you run with `pytest -s`
    print(f"\n[DEBUG] API Response Body: {body}")
    
    assert body.get("success") is True, f"'success' flag was false. Full body: {body}"
    assert "data" in body, "Response missing 'data' key"
    assert isinstance(body["data"], dict), f"Expected 'data' to be dict, got {type(body['data'])}"
    assert "message" in body["data"], "Response data missing 'message' field"