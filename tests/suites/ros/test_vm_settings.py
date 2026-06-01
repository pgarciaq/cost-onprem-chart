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


def _vm_terms_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/settings/vm/terms"
    )


def _delete_vm_settings(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
) -> requests.Response:
    return session.delete(_vm_settings_url(ros_api_url), headers=auth, timeout=60)


def _delete_vm_terms(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
) -> requests.Response:
    return session.delete(_vm_terms_url(ros_api_url), headers=auth, timeout=60)


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

    def test_vm_adaptive_margin_in_settings(
        self,
        ros_api_url: str,
        vm_settings_auth: dict,
        http_session: requests.Session,
    ):
        """GET /settings/vm exposes cpu_adaptive_margin_enabled (default CI)."""
        resp = _fetch_vm_settings(http_session, ros_api_url, vm_settings_auth)
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "cpu_adaptive_margin_enabled" in body
        assert isinstance(body["cpu_adaptive_margin_enabled"], bool)

    def test_vm_windows_kernel_reserve_in_settings(
        self,
        ros_api_url: str,
        vm_settings_auth: dict,
        http_session: requests.Session,
    ):
        """GET /settings/vm exposes windows_kernel_reserve_gib (default CI)."""
        resp = _fetch_vm_settings(http_session, ros_api_url, vm_settings_auth)
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        floors = resp.json().get("memory_floors") or {}
        assert "windows_kernel_reserve_gib" in floors
        assert isinstance(floors["windows_kernel_reserve_gib"], (int, float))

    def test_vm_history_retention_days_in_settings(
        self,
        ros_api_url: str,
        vm_settings_auth: dict,
        http_session: requests.Session,
    ):
        """GET /settings/vm exposes read-only history_retention_days (default CI)."""
        resp = _fetch_vm_settings(http_session, ros_api_url, vm_settings_auth)
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "history_retention_days" in body
        assert isinstance(body["history_retention_days"], int)
        assert body["history_retention_days"] >= 1

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

    def test_vm_settings_delete_resets(
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
        if "disk.projection_window_days" in locked:
            pytest.skip("VM disk settings are env-locked on this cluster")

        custom_window = 21
        if baseline.get("disk", {}).get("projection_window_days") == custom_window:
            custom_window = 22

        put_resp = _put_vm_settings(
            http_session,
            ros_api_url,
            vm_settings_auth,
            {"disk": {"projection_window_days": custom_window}},
        )
        if put_resp.status_code == 403:
            pytest.skip("VM settings PUT rejected (env-locked or read-only)")
        assert put_resp.status_code == 200, put_resp.text
        assert put_resp.json()["disk"]["projection_window_days"] == custom_window

        del_resp = _delete_vm_settings(http_session, ros_api_url, vm_settings_auth)
        assert del_resp.status_code in (200, 204), del_resp.text

        restored = _fetch_vm_settings(http_session, ros_api_url, vm_settings_auth)
        assert restored.status_code == 200, restored.text
        assert restored.json()["disk"]["projection_window_days"] == baseline["disk"][
            "projection_window_days"
        ]

    def test_vm_terms_delete_resets(
        self,
        ros_api_url: str,
        vm_settings_auth: dict,
        http_session: requests.Session,
    ):
        terms_url = _vm_terms_url(ros_api_url)
        get_resp = http_session.get(terms_url, headers=vm_settings_auth, timeout=30)
        skip_if_vm_plugin_disabled(get_resp)
        assert get_resp.status_code == 200, get_resp.text
        baseline = get_resp.json()
        if any(t.get("locked") for t in baseline.get("terms") or []):
            pytest.skip("VM terms are locked on this cluster")

        put_resp = http_session.put(
            terms_url,
            headers={**vm_settings_auth, "Content-Type": "application/json"},
            json={"terms": [{"name": "short_term", "window_days": 11}]},
            timeout=60,
        )
        if put_resp.status_code == 403:
            pytest.skip("VM terms PUT rejected (env-locked or read-only)")
        assert put_resp.status_code == 200, put_resp.text

        del_resp = _delete_vm_terms(http_session, ros_api_url, vm_settings_auth)
        assert del_resp.status_code in (200, 204), del_resp.text

        restored = http_session.get(terms_url, headers=vm_settings_auth, timeout=30)
        assert restored.status_code == 200, restored.text
        short_baseline = next(
            t for t in baseline["terms"] if t["name"] == "short_term"
        )
        short_restored = next(
            t for t in restored.json()["terms"] if t["name"] == "short_term"
        )
        assert short_restored["window_days"] == short_baseline["window_days"]

    def test_vm_gpu_classification_settings(
        self,
        ros_api_url: str,
        vm_settings_auth: dict,
        http_session: requests.Session,
    ):
        """GET/PUT/DELETE for gpu classification thresholds in /settings/vm."""
        resp = _fetch_vm_settings(http_session, ros_api_url, vm_settings_auth)
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        gpu = resp.json().get("gpu") or {}
        assert gpu.get("idle_threshold_bp") is not None
        assert gpu.get("underutil_threshold_bp") is not None
        assert gpu.get("fb_saturation_mib") is not None
        assert gpu.get("compute_saturation_threshold_bp") is not None

        baseline = resp.json()
        locked = set(baseline.get("locked_fields") or [])
        if "gpu.idle_threshold_bp" in locked:
            pytest.skip("gpu.idle_threshold_bp is env-locked on this cluster")

        custom_idle = 1200
        if baseline.get("gpu", {}).get("idle_threshold_bp") == custom_idle:
            custom_idle = 1300

        put_resp = _put_vm_settings(
            http_session,
            ros_api_url,
            vm_settings_auth,
            {
                "gpu": {
                    **baseline.get("gpu", {}),
                    "idle_threshold_bp": custom_idle,
                }
            },
        )
        if put_resp.status_code == 403:
            pytest.skip("VM settings PUT rejected (env-locked or read-only)")
        assert put_resp.status_code == 200, put_resp.text
        assert put_resp.json()["gpu"]["idle_threshold_bp"] == custom_idle

        verify = _fetch_vm_settings(http_session, ros_api_url, vm_settings_auth)
        assert verify.status_code == 200, verify.text
        assert verify.json()["gpu"]["idle_threshold_bp"] == custom_idle

        del_resp = _delete_vm_settings(http_session, ros_api_url, vm_settings_auth)
        assert del_resp.status_code in (200, 204), del_resp.text

        restored = _fetch_vm_settings(http_session, ros_api_url, vm_settings_auth)
        assert restored.status_code == 200, restored.text
        assert restored.json()["gpu"]["idle_threshold_bp"] == baseline["gpu"][
            "idle_threshold_bp"
        ]
