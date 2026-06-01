"""Lightweight VM API contract smoke tests (default CI suite).

These run on every PR (not ``extended``) and verify VM routes and query parameters
respond without requiring seeded notification/GPU/preference scenarios. Full behavioral
coverage lives under ``tests/suites/e2e/test_vm_*.py`` (``--extended``).

Run:
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --ros -k test_vm_smoke
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token
from suites.ros.test_vm_recommendations import _fetch_vm_list, skip_if_vm_plugin_disabled


def _vm_history_url(ros_api_url: str, vm_name: str) -> str:
    base = ros_api_url.rstrip("/")
    return (
        f"{base}/cost-management/v1/recommendations/openshift/vms/"
        f"{vm_name}/history"
    )


def _fetch_vm_history(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    vm_name: str,
    params: dict[str, Any],
) -> requests.Response:
    return session.get(
        _vm_history_url(ros_api_url, vm_name),
        headers=auth,
        params=params,
        timeout=60,
    )


@pytest.fixture
def vm_smoke_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.vm
@pytest.mark.smoke
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestVMSmokeContract:
    """VM recommendation API surface checks without extended NISE fixtures."""

    def test_vm_gpu_filter_returns_200(
        self,
        ros_api_url: str,
        vm_smoke_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_smoke_auth,
            {"filter[has_gpu]": "true", "limit": 5},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text

    def test_vm_gpu_filter_returns_200_false(
        self,
        ros_api_url: str,
        vm_smoke_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_smoke_auth,
            {"filter[has_gpu]": "false", "limit": 5},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text

    def test_vm_notifications_field_exists(
        self,
        ros_api_url: str,
        vm_smoke_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(http_session, ros_api_url, vm_smoke_auth, {"limit": 10})
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            return
        for row in items:
            notifications = row.get("notifications")
            assert notifications is not None, row
            assert isinstance(notifications, list), row

    def test_vm_dual_engine_filter_returns_200(
        self,
        ros_api_url: str,
        vm_smoke_auth: dict,
        http_session: requests.Session,
    ):
        for engine in ("cost", "performance"):
            resp = _fetch_vm_list(
                http_session,
                ros_api_url,
                vm_smoke_auth,
                {"filter[engine]": engine, "limit": 5},
            )
            skip_if_vm_plugin_disabled(resp)
            assert resp.status_code == 200, resp.text

    def test_vm_confidence_filter_returns_200(
        self,
        ros_api_url: str,
        vm_smoke_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_smoke_auth,
            {"filter[confidence]": "high", "limit": 5},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text

    def test_vm_history_endpoint_returns_200(
        self,
        ros_api_url: str,
        vm_smoke_auth: dict,
        http_session: requests.Session,
    ):
        list_resp = _fetch_vm_list(
            http_session, ros_api_url, vm_smoke_auth, {"limit": 5}
        )
        skip_if_vm_plugin_disabled(list_resp)
        assert list_resp.status_code == 200, list_resp.text
        items = list_resp.json().get("data") or []
        vm_name = "smoke-placeholder-vm"
        namespace = "default"
        cluster_uuid: Optional[str] = None
        if items:
            vm_name = items[0]["vm_name"]
            namespace = items[0]["namespace"]
            cluster_uuid = items[0]["cluster_uuid"]
        else:
            pytest.skip("No VM rows for history smoke; list contract still validated above")

        params: dict[str, Any] = {
            "namespace": namespace,
            "term": "short_term",
            "engine": "cost",
            "limit": 5,
            "offset": 0,
        }
        if cluster_uuid:
            params["cluster_uuid"] = cluster_uuid

        hist = _fetch_vm_history(
            http_session, ros_api_url, vm_smoke_auth, vm_name, params
        )
        assert hist.status_code == 200, hist.text
        body = hist.json()
        assert "data" in body
        assert isinstance(body["data"], list)
        assert "meta" in body
