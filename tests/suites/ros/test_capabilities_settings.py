"""E2E tests for ROS settings capabilities API.

Endpoint:
  GET /api/cost-management/v1/recommendations/openshift/settings/capabilities
"""

from __future__ import annotations

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token


def _capabilities_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/settings/capabilities"
    )


@pytest.fixture
def capabilities_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.component
class TestCapabilitiesSettingsE2E:
    """Capabilities discovery endpoint."""

    def test_capabilities_get_returns_expected_shape(
        self,
        ros_api_url: str,
        capabilities_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _capabilities_url(ros_api_url),
            headers=capabilities_auth,
            timeout=30,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert "recommendation_types" in body
        assert isinstance(body["recommendation_types"], list)
        assert body["recommendation_types"], "expected at least one recommendation type"

        first = body["recommendation_types"][0]
        for key in ("name", "supports_terms", "enabled"):
            assert key in first, f"capability item missing {key!r}"

        assert "business_hours" in body
        assert isinstance(body["business_hours"], bool)
