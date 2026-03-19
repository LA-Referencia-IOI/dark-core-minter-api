"""
Tests for authority and health endpoints.
"""


def test_health_check(client, mock_corelib):
    """Test health endpoint returns blockchain status."""
    mock_corelib.is_connected.return_value = True
    mock_corelib.get_block_number.return_value = 12345

    response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["blockchain_connected"] is True
    assert data["current_block"] == 12345


def test_get_authority_success(client, mock_corelib, mock_authority_info):
    """Test getting authority information by UUID."""
    mock_corelib.get_authority_by_uuid.return_value = mock_authority_info

    response = client.get("/api/v1/authority/test-authority-uuid")

    assert response.status_code == 200
    data = response.json()
    assert data["uuid"] == mock_authority_info.uuid
    assert data["wallet_address"] == mock_authority_info.wallet_address
    assert data["naans"] == mock_authority_info.naans
    assert data["active"] is True


def test_get_authority_naans_success(client, mock_corelib):
    """Test getting authorized NAANs for an authority."""
    mock_corelib.get_authorized_naans.return_value = ["12345", "67890"]

    response = client.get("/api/v1/authority/test-authority-uuid/naans")

    assert response.status_code == 200
    data = response.json()
    assert data["uuid"] == "test-authority-uuid"
    assert data["naans"] == ["12345", "67890"]


def test_check_authority_authorization_true(client, mock_corelib):
    """Test checking NAAN authorization for an authority."""
    mock_corelib.is_authorized_for_naan.return_value = True

    response = client.get("/api/v1/authority/test-authority-uuid/authorized/12345")

    assert response.status_code == 200
    data = response.json()
    assert data["uuid"] == "test-authority-uuid"
    assert data["naan"] == "12345"
    assert data["authorized"] is True
