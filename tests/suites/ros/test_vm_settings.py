"""E2E tests for ROS VM recommendation settings API.

Endpoint: GET/PUT /api/cost-management/v1/recommendations/openshift/settings/vm
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token
from suites.ros.test_vm_recommendations import skip_if_vm_plugin_disabled


def _vm_settings_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/settings/vm"
    )


def _fetch_vm_settings(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
) -> requests.Response:
    return session.get(_vm_settings_url(ros_api_url), headers=auth, timeout=30)


def _put_vm_settings(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    body: dict[str, Any],
) -> requests.Response:
    return session.put(
        _vm_settings_url(ros_api_url),
        headers={**auth, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )


@pytest.fixture
def vm_settings_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.vm
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestVMSettingsE2E:
    """VM recommendation settings defaults and optional tenant overrides."""

    def test_vm_settings_get_defaults(
        self,
        ros_api_url: str,
        vm_settings_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_settings(http_session, ros_api_url, vm_settings_auth)
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "thresholds" in body
        assert "disk" in body
        assert "io" in body
        assert "enabled" in body
        thresholds = body["thresholds"]
        assert thresholds.get("cpu_percentile_cost") is not None
        assert thresholds.get("idle_cpu_mc") is not None

    def test_vm_settings_put_optional(
        self,
        ros_api_url: str,
        vm_settings_auth: dict,
        http_session: requests.Session,
    ):
        get_resp = _fetch_vm_settings(http_session, ros_api_url, vm_settings_auth)
        skip_if_vm_plugin_disabled(get_resp)
        assert get_resp.status_code == 200, get_resp.text
        baseline = get_resp.json()
        locked = set(baseline.get("locked_fields") or [])
        if "thresholds.cpu_percentile_cost" in locked:
            pytest.skip("VM settings are env-locked on this cluster")

        new_cost = 0.55
        if baseline.get("thresholds", {}).get("cpu_percentile_cost") == new_cost:
            new_cost = 0.56

        put_body = {
            "thresholds": {
                **baseline.get("thresholds", {}),
                "cpu_percentile_cost": new_cost,
            }
        }
        put_resp = _put_vm_settings(
            http_session, ros_api_url, vm_settings_auth, put_body
        )
        if put_resp.status_code == 403:
            pytest.skip("VM settings PUT rejected (env-locked or read-only)")
        assert put_resp.status_code == 200, put_resp.text
        updated = put_resp.json()
        assert updated["thresholds"]["cpu_percentile_cost"] == new_cost
