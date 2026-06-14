"""
Lightweight performance smoke tests for default CI.

Validates critical API endpoints respond within acceptable latency without
running the full performance suite (ingestion, scale, soak).
"""

from __future__ import annotations

import time

import pytest
import requests

from conftest import get_fresh_auth_header
from suites.ros.test_recommendations import get_recommendations_endpoint


CONTAINER_LIST_MAX_SECONDS = 5.0
STATUS_MAX_SECONDS = 2.0


@pytest.mark.ros
@pytest.mark.smoke
@pytest.mark.smoke_perf
class TestROSSmokePerformance:
    """Fast latency checks included in default CI (~3 min)."""

    def test_container_list_responds_within_threshold(
        self,
        ros_api_url: str,
        keycloak_config,
        http_session: requests.Session,
    ):
        """Container list endpoint should respond within 5 seconds."""
        auth_header = get_fresh_auth_header(keycloak_config, http_session)
        if not auth_header:
            pytest.skip("Could not obtain fresh JWT token")

        url = get_recommendations_endpoint(ros_api_url)
        start = time.monotonic()
        response = http_session.get(
            url,
            headers=auth_header,
            params={"limit": 10, "offset": 0},
            timeout=CONTAINER_LIST_MAX_SECONDS + 2,
        )
        elapsed = time.monotonic() - start

        assert response.status_code in (200, 404), (
            f"Unexpected status {response.status_code}: {response.text[:200]}"
        )
        assert elapsed < CONTAINER_LIST_MAX_SECONDS, (
            f"Container list took {elapsed:.2f}s (threshold {CONTAINER_LIST_MAX_SECONDS}s)"
        )

    def test_ros_status_responds_within_threshold(
        self,
        ros_api_url: str,
        keycloak_config,
        http_session: requests.Session,
    ):
        """ROS status endpoint should respond within 2 seconds."""
        auth_header = get_fresh_auth_header(keycloak_config, http_session)
        if not auth_header:
            pytest.skip("Could not obtain fresh JWT token")

        url = f"{ros_api_url.rstrip('/')}/cost-management/v1/status/"
        start = time.monotonic()
        response = http_session.get(
            url,
            headers=auth_header,
            timeout=STATUS_MAX_SECONDS + 2,
        )
        elapsed = time.monotonic() - start

        assert response.status_code == 200, (
            f"Status endpoint returned {response.status_code}: {response.text[:200]}"
        )
        assert elapsed < STATUS_MAX_SECONDS, (
            f"Status took {elapsed:.2f}s (threshold {STATUS_MAX_SECONDS}s)"
        )
