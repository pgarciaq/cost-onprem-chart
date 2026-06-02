"""E2E tests for ROS idle-detection settings API.

Endpoints:
  GET/PUT/DELETE /api/cost-management/v1/recommendations/openshift/settings/idle-detection
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token

_DEFAULT_IDLE = {
    "enabled": True,
    "thresholds": {
        "cpu_utilization_percent": 2,
        "memory_utilization_percent": 5,
        "burst_ratio": 10,
        "minimum_observation_days": 14,
        "gpu_sm_active_basis_points": 500,
        "gpu_dram_active_basis_points": 500,
    },
}


def _idle_detection_settings_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/settings/idle-detection"
    )


def _get_settings(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
) -> requests.Response:
    return session.get(
        _idle_detection_settings_url(ros_api_url),
        headers=auth,
        timeout=30,
    )


def _put_settings(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    body: dict[str, Any],
) -> requests.Response:
    return session.put(
        _idle_detection_settings_url(ros_api_url),
        headers={**auth, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )


def _delete_settings(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
) -> requests.Response:
    return session.delete(
        _idle_detection_settings_url(ros_api_url),
        headers=auth,
        timeout=60,
    )


def _idle_block(body: dict[str, Any]) -> dict[str, Any]:
    idle = body.get("idle_detection")
    assert isinstance(idle, dict), "response missing idle_detection object"
    return idle


@pytest.fixture
def idle_detection_settings_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.component
class TestIdleDetectionSettingsE2E:
    """Idle-detection settings CRUD against a deployed cluster."""

    def test_idle_detection_settings_get_defaults(
        self,
        ros_api_url: str,
        idle_detection_settings_auth: dict,
        http_session: requests.Session,
    ):
        resp = _get_settings(
            http_session, ros_api_url, idle_detection_settings_auth
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        idle = _idle_block(body)
        assert idle["enabled"] is True
        thresholds = idle["thresholds"]
        assert thresholds["cpu_utilization_percent"] == 2
        assert thresholds["memory_utilization_percent"] == 5
        assert thresholds["burst_ratio"] == 10
        assert thresholds["minimum_observation_days"] == 14
        assert isinstance(body.get("locked_fields"), list)

    def test_idle_detection_settings_put_persists(
        self,
        ros_api_url: str,
        idle_detection_settings_auth: dict,
        http_session: requests.Session,
        keycloak_config,
        cluster_config,
    ):
        custom = {
            "idle_detection": {
                "enabled": False,
                "thresholds": {"cpu_utilization_percent": 3},
            }
        }
        try:
            put_resp = _put_settings(
                http_session,
                ros_api_url,
                idle_detection_settings_auth,
                custom,
            )
            assert put_resp.status_code == 200, put_resp.text
            put_idle = _idle_block(put_resp.json())
            assert put_idle["enabled"] is False
            assert put_idle["thresholds"]["cpu_utilization_percent"] == 3

            get_resp = _get_settings(
                http_session, ros_api_url, idle_detection_settings_auth
            )
            assert get_resp.status_code == 200, get_resp.text
            get_idle = _idle_block(get_resp.json())
            assert get_idle["enabled"] is False
            assert get_idle["thresholds"]["cpu_utilization_percent"] == 3
        finally:
            auth = get_fresh_token(keycloak_config, cluster_config, http_session)
            if auth:
                _delete_settings(http_session, ros_api_url, auth)

    def test_idle_detection_settings_delete_resets_defaults(
        self,
        ros_api_url: str,
        idle_detection_settings_auth: dict,
        http_session: requests.Session,
    ):
        put_resp = _put_settings(
            http_session,
            ros_api_url,
            idle_detection_settings_auth,
            {"idle_detection": {"enabled": False}},
        )
        assert put_resp.status_code == 200, put_resp.text

        del_resp = _delete_settings(
            http_session, ros_api_url, idle_detection_settings_auth
        )
        assert del_resp.status_code == 200, del_resp.text
        del_idle = _idle_block(del_resp.json())
        assert del_idle["enabled"] is True

        get_resp = _get_settings(
            http_session, ros_api_url, idle_detection_settings_auth
        )
        assert get_resp.status_code == 200, get_resp.text
        get_idle = _idle_block(get_resp.json())
        assert get_idle["enabled"] is True
        assert (
            get_idle["thresholds"]["cpu_utilization_percent"]
            == _DEFAULT_IDLE["thresholds"]["cpu_utilization_percent"]
        )

    def test_idle_detection_settings_put_validation_cpu_out_of_range(
        self,
        ros_api_url: str,
        idle_detection_settings_auth: dict,
        http_session: requests.Session,
    ):
        resp = _put_settings(
            http_session,
            ros_api_url,
            idle_detection_settings_auth,
            {
                "idle_detection": {
                    "thresholds": {"cpu_utilization_percent": 100},
                }
            },
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("status") == "error"
        assert body.get("validation_errors")

    def test_idle_detection_settings_put_validation_invalid_workload_type(
        self,
        ros_api_url: str,
        idle_detection_settings_auth: dict,
        http_session: requests.Session,
    ):
        resp = _put_settings(
            http_session,
            ros_api_url,
            idle_detection_settings_auth,
            {
                "idle_detection": {
                    "exclusions": {"workload_types": ["NotARealKind"]},
                }
            },
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("status") == "error"
        assert body.get("validation_errors")
