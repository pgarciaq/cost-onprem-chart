"""E2E tests for ROS savings estimation endpoints."""

from __future__ import annotations

import time
from typing import Any

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token, get_recommendations_endpoint
from utils import (
    assert_structured_savings,
    normalize_org_id as _bare_org_id,
    parse_savings_value,
    run_oc_command,
)

_FLOAT_TOLERANCE = 0.02


def _savings_summary_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/savings-summary"
    )


def _recalculate_savings_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "internal/recalculate-savings"
    )


def _fleet_summary_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/fleet-summary"
    )


def _pvcs_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/pvcs"
    )


def _has_container_recommendations(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
) -> bool:
    resp = session.get(
        get_recommendations_endpoint(ros_api_url),
        headers=auth,
        params={"limit": 1},
        timeout=60,
    )
    if resp.status_code != 200:
        return False
    return bool(resp.json().get("data"))


def _plugin_sum(by_plugin: dict[str, Any]) -> float:
    """Sum plugin savings included in fleet total (GPU excluded at read time)."""
    total = 0.0
    for key in ("container", "node", "pvc", "snapshot", "vm"):
        value = by_plugin.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            parsed = parse_savings_value(value)
            if parsed is not None:
                total += parsed
        else:
            total += float(value)
    return total


@pytest.fixture
def savings_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.fixture
def require_recommendations(
    ros_api_url: str,
    savings_auth: dict,
    http_session: requests.Session,
):
    """Skip savings/fleet tests when the cluster has no container recommendations yet."""
    if not _has_container_recommendations(http_session, ros_api_url, savings_auth):
        pytest.skip(
            "No container recommendations in cluster; ingest ROS data before running "
            "savings summary E2E tests"
        )


@pytest.mark.ros
@pytest.mark.integration
class TestSavingsSummaryE2E:
    """Fleet savings and fleet summary endpoints."""

    @pytest.mark.timeout(60)
    def test_savings_summary_returns_valid_structure(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "currency" in body
        assert "estimated_monthly_savings" in body
        assert_structured_savings(body["estimated_monthly_savings"])
        assert "by_cluster" in body
        assert "by_plugin" in body
        assert isinstance(body["by_cluster"], list)
        assert isinstance(body["by_plugin"], dict)

    @pytest.mark.timeout(60)
    def test_savings_summary_by_plugin_has_expected_keys(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        by_plugin = resp.json()["by_plugin"]
        for key in ("container", "node", "pvc", "vm"):
            assert key in by_plugin, f"by_plugin missing required key {key!r}"
        # snapshot/gpu may be zero depending on cluster data
        for optional in ("snapshot", "gpu"):
            if optional in by_plugin and by_plugin[optional] is not None:
                assert_structured_savings(by_plugin[optional])
        for key in ("container", "node", "pvc", "vm"):
            if key in by_plugin and by_plugin[key] is not None:
                assert_structured_savings(by_plugin[key])

    @pytest.mark.timeout(60)
    def test_savings_summary_total_matches_plugin_sum(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        total = parse_savings_value(body["estimated_monthly_savings"])
        assert total is not None
        plugin_total = _plugin_sum(body["by_plugin"])
        assert total == pytest.approx(plugin_total, abs=_FLOAT_TOLERANCE), (
            f"total {total} != plugin sum {plugin_total} (by_plugin={body['by_plugin']})"
        )

    @pytest.mark.timeout(60)
    def test_savings_summary_engine_cost_default(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        default_resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            timeout=60,
        )
        explicit_resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            params={"engine": "cost"},
            timeout=60,
        )
        assert default_resp.status_code == 200, default_resp.text
        assert explicit_resp.status_code == 200, explicit_resp.text
        assert default_resp.json() == explicit_resp.json()

    @pytest.mark.timeout(60)
    def test_savings_summary_engine_performance_accepted(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            params={"engine": "performance"},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "estimated_monthly_savings" in body
        assert_structured_savings(body["estimated_monthly_savings"])
        assert "by_plugin" in body

    @pytest.mark.timeout(60)
    def test_savings_summary_invalid_engine_returns_400(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            params={"engine": "invalid"},
            timeout=60,
        )
        assert resp.status_code == 400, resp.text
        assert resp.json().get("status") == "error"

    @pytest.mark.timeout(60)
    def test_fleet_summary_returns_valid_structure(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _fleet_summary_url(ros_api_url),
            headers=savings_auth,
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for field in (
            "total_containers",
            "active_containers",
            "idle_containers",
            "abandoned_containers",
            "cluster_count",
            "currency",
        ):
            assert field in body, f"missing {field!r} in fleet-summary response"

    @pytest.mark.timeout(60)
    def test_fleet_summary_counts_consistent(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _fleet_summary_url(ros_api_url),
            headers=savings_auth,
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        total = int(body["total_containers"])
        active = int(body["active_containers"])
        idle = int(body["idle_containers"])
        abandoned = int(body["abandoned_containers"])
        assert active + idle + abandoned <= total, (
            f"active({active}) + idle({idle}) + abandoned({abandoned}) "
            f"exceeds total_containers({total})"
        )


@pytest.mark.component
def test_savings_summary_filter_cluster(
    ros_api_url: str,
    savings_auth: dict,
    http_session: requests.Session,
):
    """Fleet savings-summary filter[cluster] scopes grouped responses."""
    url = _savings_summary_url(ros_api_url)
    group_params = {"group_by[idle_state]": "*"}

    unfiltered = http_session.get(
        url,
        headers=savings_auth,
        params=group_params,
        timeout=60,
    )
    assert unfiltered.status_code == 200, unfiltered.text
    unfiltered_body = unfiltered.json()
    assert "data" in unfiltered_body
    assert "meta" in unfiltered_body

    filtered = http_session.get(
        url,
        headers=savings_auth,
        params={
            **group_params,
            "filter[cluster]": "00000000-0000-0000-0000-000000000000",
        },
        timeout=60,
    )
    assert filtered.status_code == 200, filtered.text
    filtered_body = filtered.json()
    assert "data" in filtered_body

    # Non-existent cluster should yield empty grouped response
    assert filtered_body["data"] == [] or int(filtered_body.get("meta", {}).get("count", -1)) == 0

    if isinstance(filtered_body["data"], list) and filtered_body["data"]:
        for item in filtered_body["data"]:
            waste_val = item.get("estimated_monthly_waste", {})
            if isinstance(waste_val, dict):
                assert float(waste_val.get("value", 0)) == 0

    if unfiltered_body["data"]:
        unfiltered_waste = sum(
            parse_savings_value(row.get("estimated_monthly_waste")) or 0
            for row in unfiltered_body["data"]
            if isinstance(row, dict)
        )
        filtered_waste = sum(
            parse_savings_value(row.get("estimated_monthly_waste")) or 0
            for row in filtered_body["data"]
            if isinstance(row, dict)
        )
        assert filtered_waste <= unfiltered_waste + _FLOAT_TOLERANCE


@pytest.mark.component
def test_savings_summary_group_by_tag(
    require_recommendations,
    ros_api_url: str,
    savings_auth: dict,
    http_session: requests.Session,
):
    """Fleet savings-summary supports group_by[tag:*] for tag-based breakdown."""
    resp = http_session.get(
        _savings_summary_url(ros_api_url),
        headers=savings_auth,
        params={"group_by[tag:environment]": "*"},
        timeout=60,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    if "data" not in data and "by_cluster" in data:
        pytest.skip(
            "Tag-grouped savings summary unavailable; ROS_TAGS_ENABLED may be false "
            "or group_by[tag:*] was ignored"
        )
    assert "data" in data
    assert "meta" in data


@pytest.mark.extended
def test_savings_summary_filter_project_with_tag_groupby(
    require_recommendations,
    ros_api_url: str,
    savings_auth: dict,
    http_session: requests.Session,
):
    """Fleet savings-summary filter[project] scopes tag-grouped container rollups."""
    namespace = "payments"
    list_resp = http_session.get(
        get_recommendations_endpoint(ros_api_url),
        headers=savings_auth,
        params={"limit": 1},
        timeout=60,
    )
    if list_resp.status_code == 200 and list_resp.json().get("data"):
        namespace = list_resp.json()["data"][0].get("project") or namespace

    resp = http_session.get(
        _savings_summary_url(ros_api_url),
        headers=savings_auth,
        params={
            "group_by[tag:environment]": "*",
            "filter[project]": namespace,
        },
        timeout=60,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "data" in data
    assert "meta" in data
    assert isinstance(data["data"], list)
    assert "count" in data["meta"]


@pytest.mark.component
def test_savings_summary_group_by_idle_state(
    require_recommendations,
    ros_api_url: str,
    savings_auth: dict,
    http_session: requests.Session,
):
    """Fleet savings-summary supports group_by[idle_state] for idle/active breakdown."""
    resp = http_session.get(
        _savings_summary_url(ros_api_url),
        headers=savings_auth,
        params={"group_by[idle_state]": "*"},
        timeout=60,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "data" in data
    assert "meta" in data


@pytest.mark.component
def test_savings_summary_term_filter(
    require_recommendations,
    ros_api_url: str,
    savings_auth: dict,
    http_session: requests.Session,
):
    """Fleet savings-summary supports term parameter (short, medium, long)."""
    for term in ("short", "medium", "long"):
        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            params={"term": term},
            timeout=60,
        )
        assert resp.status_code == 200, (
            f"term={term} failed with {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "data" in data or "estimated_monthly_savings" in data


@pytest.mark.extended
def test_savings_summary_kill_switch(
    cluster_config,
    require_recommendations,
    ros_api_url: str,
    savings_auth: dict,
    http_session: requests.Session,
):
    """When ROS_SAVINGS_ESTIMATES_ENABLED=false, new savings computations are skipped.

    Toggles the env var on the ROS processor deployment, waits for rollout,
    verifies the API still serves persisted data, then restores the original value.
    """
    deployment = f"{cluster_config.helm_release_name}-ros-processor"
    ns = cluster_config.namespace
    env_var = "ROS_SAVINGS_ESTIMATES_ENABLED"

    result = run_oc_command(
        [
            "get",
            "deployment",
            deployment,
            "-n",
            ns,
            "-o",
            f"jsonpath={{.spec.template.spec.containers[0].env[?(@.name=='{env_var}')].value}}",
        ],
        check=False,
    )
    original_value = result.stdout.strip() or "true"

    try:
        run_oc_command(
            ["set", "env", f"deployment/{deployment}", f"{env_var}=false", "-n", ns],
        )
        run_oc_command(
            [
                "rollout",
                "status",
                f"deployment/{deployment}",
                "-n",
                ns,
                "--timeout=120s",
            ],
            timeout=130,
        )
        time.sleep(5)

        # The kill-switch prevents NEW savings computation during ingestion.
        # Persisted historical savings remain unchanged (served from DB).
        # Full post-ingest $0 validation would require: disable → delete existing
        # data → re-ingest → verify. This test validates the operational toggle
        # (no crash, API intact) only.
        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Response structure must stay valid even when savings estimates are disabled.
        assert "meta" in data or "data" in data or "estimated_monthly_savings" in data

    finally:
        run_oc_command(
            [
                "set",
                "env",
                f"deployment/{deployment}",
                f"{env_var}={original_value}",
                "-n",
                ns,
            ],
            check=False,
        )
        run_oc_command(
            [
                "rollout",
                "status",
                f"deployment/{deployment}",
                "-n",
                ns,
                "--timeout=120s",
            ],
            check=False,
            timeout=130,
        )


@pytest.mark.extended
def test_recalculate_savings_endpoint_smoke(
    ros_api_url: str,
    org_id: str,
    http_session: requests.Session,
):
    """POST /internal/recalculate-savings exists and accepts or rejects auth appropriately.

    Smoke test only — does not wait for async recalculation to finish.
    """
    body = {
        "org_id": _bare_org_id(org_id),
        "recommendation_types": ["container"],
    }
    resp = http_session.post(
        _recalculate_savings_url(ros_api_url),
        json=body,
        timeout=60,
    )
    assert resp.status_code in (202, 401, 404), (
        f"unexpected status {resp.status_code}: {resp.text}"
    )
    if resp.status_code == 202:
        data = resp.json()
        assert data.get("status") == "accepted"
        assert "recommendation_types" in data
    elif resp.status_code == 404:
        assert resp.json().get("status") in ("not_found", "error")


@pytest.mark.extended
def test_pvc_orphaned_savings_nonzero(
    ros_api_url: str,
    savings_auth: dict,
    http_session: requests.Session,
):
    """Orphaned PVCs should show non-zero savings (full capacity × rate).

    Requires test data with an orphaned PVC (zero usage over observation window).
    """
    resp = http_session.get(
        _pvcs_url(ros_api_url),
        headers=savings_auth,
        params={"filter[recommendation_type]": "orphaned"},
        timeout=60,
    )
    if resp.status_code == 404:
        pytest.skip("PVC recommendations plugin not enabled (404 on /pvcs)")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    if not data.get("data"):
        pytest.skip("No orphaned PVCs in test data")
    for pvc in data["data"]:
        savings_obj = pvc.get("estimated_monthly_savings")
        if savings_obj is None:
            continue
        assert_structured_savings(savings_obj)
        savings = parse_savings_value(savings_obj)
        assert savings is not None and savings > 0, (
            f"Orphaned PVC {pvc.get('pvc_name')} should have positive savings "
            f"(full capacity recoverable), got {savings_obj}"
        )
