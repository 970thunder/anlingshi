import os
import base64
import json
from pathlib import Path
from datetime import datetime, timezone
from cryptography.fernet import Fernet

TEST_DB = Path(__file__).parent / "test.sqlite3"
TEST_DB.unlink(missing_ok=True)
os.environ["MATCH_DB"] = str(TEST_DB)
os.environ["WRITE_TOKEN"] = "test-token"

from fastapi.testclient import TestClient
import api.app as app_module
from api.app import app


def test_ingest_stats_and_idempotency():
    with TestClient(app) as client:
        for index, winner in enumerate(["红", "蓝", "red", "blue"]):
            response = client.post("/api/v1/results", headers={"X-Write-Token": "test-token"}, json={"round_id": f"r-{index}", "winner": winner, "occurred_at": datetime(2026, 1, 1, 0, index, tzinfo=timezone.utc).isoformat()})
            assert response.status_code == 200
        duplicate = client.post("/api/v1/results", headers={"X-Write-Token": "test-token"}, json={"round_id": "r-0", "winner": "red"})
        assert duplicate.status_code == 200
        stats = client.get("/api/v1/stats?window=50").json()
        assert stats["total"] == 4
        assert stats["counts"] == {"red": 2, "blue": 2}
        prediction = client.get("/api/v1/prediction?window=50").json()
        assert prediction["sample_size"] == 4
        assert prediction["ready"] is False


def test_rejects_bad_token_and_winner():
    with TestClient(app) as client:
        assert client.post("/api/v1/results", json={"winner": "red"}).status_code == 401
        assert client.post("/api/v1/results", headers={"X-Write-Token": "test-token"}, json={"winner": "green"}).status_code == 422


def test_admin_login_and_encrypted_model_config(monkeypatch):
    monkeypatch.setattr(app_module, "ADMIN_PASSWORD", "admin-pass")
    monkeypatch.setattr(app_module, "ADMIN_SESSION_SECRET", "session-secret")
    monkeypatch.setattr(app_module, "ADMIN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with TestClient(app) as client:
        login = client.post("/api/v1/admin/login", json={"username": "admin", "password": "admin-pass"})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        response = client.post("/api/v1/admin/models", headers={"X-CSRF-Token": csrf}, json={"name": "test-remote", "display_name": "Test Remote", "base_url": "https://example.com/v1", "api_key": "secret-api-key", "model_name": "test", "enabled": False})
        assert response.status_code == 200
        configured = client.get("/api/v1/admin/models").json()["items"]
        model = next(item for item in configured if item["name"] == "test-remote")
        assert model["key_configured"] is True
        assert model["key_suffix"] == "-key"
        assert "secret-api-key" not in response.text


def test_paired_device_uploads_encrypted_credential(monkeypatch):
    monkeypatch.setattr(app_module, "ADMIN_PASSWORD", "device-pass")
    monkeypatch.setattr(app_module, "ADMIN_SESSION_SECRET", "session-secret")
    monkeypatch.setattr(app_module, "ADMIN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    encoded = base64.urlsafe_b64encode(json.dumps({"exp": int(datetime.now(timezone.utc).timestamp()) + 3600}).encode()).decode().rstrip("=")
    jwt = f"eyJhbGciOiJIUzI1NiJ9.{encoded}.signature"
    with TestClient(app) as client:
        assert client.get("/api/v1/admin/devices").status_code == 401
        login = client.post("/api/v1/admin/login", json={"username": "admin", "password": "device-pass"})
        csrf = login.json()["csrf_token"]
        paired = client.post("/api/v1/admin/devices", headers={"X-CSRF-Token": csrf}, json={"name": "test-host"})
        assert paired.status_code == 200
        device = paired.json()["device"]
        uploaded = client.post("/api/v1/device/credentials", headers={"X-Device-ID": device["device_id"], "X-Device-Token": paired.json()["pairing_token"]}, json={"jwt": jwt})
        assert uploaded.status_code == 200
        with app_module.connect() as db:
            stored = db.execute("SELECT jwt_ciphertext FROM collector_credentials WHERE device_id=?", (device["device_id"],)).fetchone()[0]
        assert jwt not in stored
