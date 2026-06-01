"""E2E tests for ROS OpenShift Virtualization (VM) recommendation APIs.

Endpoints:
  GET /api/cost-management/v1/recommendations/openshift/vm
  GET /api/cost-management/v1/recommendations/openshift/vm/detail

Requires ros-ocp-backend VM plugin enabled (Helm ros.api.enabledPlugins includes vm).
When disabled the API returns 404 and tests skip.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token

VALID_VM_CONFIDENCE = frozenset({"high", "moderate", "low"})


def _vm_list_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/vm"
    )


def _vm_detail_url(ros_api_url: str) -> str:
    return f"{_vm_list_url(ros_api_url)}/detail"


def _fetch_vm_list(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    return session.get(
        _vm_list_url(ros_api_url),
        headers=auth,
        params=params or {},
        timeout=60,
    )


def _fetch_vm_detail(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: dict[str, Any],
) -> requests.Response:
    return session.get(
        _vm_detail_url(ros_api_url),
        headers=auth,
        params=params,
        timeout=60,
    )


def skip_if_vm_plugin_disabled(resp: requests.Response) -> None:
    if resp.status_code == 404:
        pytest.skip("VM recommendations plugin not enabled (404 on /vm)")


def vm_item_metadata(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("metadata") or {}


@pytest.fixture
def vm_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.vm
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestVMRecommendationsE2E:
    """VM recommendation list and detail against a deployed cluster."""

    def test_vm_list_envelope(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 10})
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert "links" in body
        assert isinstance(body["data"], list)

    def test_vm_list_required_fields(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 5})
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No VM recommendation data in cluster")

        item = body["data"][0]
        assert item.get("vm_name")
        assert item.get("namespace")
        assert item.get("cluster_uuid")
        meta = vm_item_metadata(item)
        assert "guest_agent_detected" in meta
        assert meta.get("confidence") in VALID_VM_CONFIDENCE

    def test_vm_list_current_and_recommended(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 5})
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No VM recommendation data in cluster")

        item = body["data"][0]
        current = item.get("current") or {}
        recommended = item.get("recommended") or {}
        assert "vcpu" in current
        assert "memory_gib" in current
        assert "vcpu" in recommended
        assert "memory_gib" in recommended

    def test_vm_filter_by_vm_name(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 5})
        skip_if_vm_plugin_disabled(baseline)
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No VM recommendation data in cluster")

        vm_name = items[0]["vm_name"]
        filtered = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_auth,
            {f"filter[vm_name]": vm_name, "limit": 10},
        )
        assert filtered.status_code == 200, filtered.text
        for row in filtered.json().get("data") or []:
            assert row.get("vm_name") == vm_name

    def test_vm_filter_by_namespace(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 5})
        skip_if_vm_plugin_disabled(baseline)
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No VM recommendation data in cluster")

        namespace = items[0]["namespace"]
        filtered = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_auth,
            {f"filter[namespace]": namespace, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for row in filtered.json().get("data") or []:
            assert row.get("namespace") == namespace

    def test_vm_recommendations_exist(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        """Default CI: VM list returns 200 and plugin is reachable (data optional)."""
        resp = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 1})
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert body["meta"].get("count", 0) >= 0

    def test_vm_detail_preference_fields_when_configured(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        """When cluster preferences are ingested, detail exposes preference metadata."""
        resp = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 100})
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []
        if not rows:
            pytest.skip("No VM recommendation data in cluster")

        for row in rows:
            meta = row.get("metadata") or {}
            if not meta.get("preference_name"):
                continue
            detail = _fetch_vm_detail(
                http_session,
                ros_api_url,
                vm_auth,
                {
                    "vm_name": row["vm_name"],
                    "namespace": row["namespace"],
                    "cluster_uuid": row["cluster_uuid"],
                },
            )
            assert detail.status_code == 200, detail.text
            detail_meta = detail.json().get("metadata") or {}
            assert detail_meta.get("preference_name")
            assert detail_meta.get("preference_class")
            return
        pytest.skip(
            "No VM with preference_name in list; upload cluster_instance_types.json "
            "with vm_preferences (see test_vm_preference_flow extended E2E)"
        )
