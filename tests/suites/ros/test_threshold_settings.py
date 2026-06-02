"""E2E tests for ROS threshold settings API."""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token, get_recommendations_endpoint
from utils import parse_savings_value, run_oc_command, wait_for_condition

_RECOMMENDATION_TYPES = ("container", "namespace", "node", "gpu", "pvc")

_TYPE_EXPECTED_FIELDS: dict[str, list[str]] = {
    "container": ["cpu_cost_percentile", "mem_cost_percentile", "locked_fields"],
    "namespace": ["cpu_cost_percentile", "mem_cost_percentile", "locked_fields"],
    "node": ["cost_target_utilization", "underutil_threshold", "locked_fields"],
    "gpu": ["idle_threshold", "underutilized_sm_threshold", "locked_fields"],
    "pvc": ["oversized_threshold", "near_full_threshold", "locked_fields"],
}

_CONTAINER_DEFAULTS = {
    "cpu_cost_percentile": 0.60,
    "min_margin": 1.15,
}

_NODE_DEFAULTS = {
    "cost_target_utilization": 0.80,
    "underutil_threshold": 0.30,
}

_NAMESPACE_DEFAULTS = {
    "cpu_cost_percentile": 0.60,
}

_GPU_DEFAULTS = {
    "idle_threshold": 0.02,
}

_PVC_DEFAULTS = {
    "oversized_threshold": 0.20,
}

# recommendation_type -> (PUT body, field to assert on GET, default after DELETE)
_THRESHOLD_PUT_CASES: list[tuple[str, dict[str, float], str, float]] = [
    ("container", {"cpu_cost_percentile": 0.72}, "cpu_cost_percentile", 0.72),
    ("node", {"cost_target_utilization": 0.75}, "cost_target_utilization", 0.75),
    ("namespace", {"cpu_cost_percentile": 0.72}, "cpu_cost_percentile", 0.72),
    ("gpu", {"idle_threshold": 0.05}, "idle_threshold", 0.05),
    ("pvc", {"oversized_threshold": 0.25}, "oversized_threshold", 0.25),
]

_THRESHOLD_DELETE_CASES: list[tuple[str, dict[str, float], str, float]] = [
    ("container", {"min_margin": 1.25}, "min_margin", _CONTAINER_DEFAULTS["min_margin"]),
    ("node", {"cost_target_utilization": 0.75}, "cost_target_utilization", _NODE_DEFAULTS["cost_target_utilization"]),
    ("namespace", {"cpu_cost_percentile": 0.72}, "cpu_cost_percentile", _NAMESPACE_DEFAULTS["cpu_cost_percentile"]),
    ("gpu", {"idle_threshold": 0.05}, "idle_threshold", _GPU_DEFAULTS["idle_threshold"]),
    ("pvc", {"oversized_threshold": 0.25}, "oversized_threshold", _PVC_DEFAULTS["oversized_threshold"]),
]

# Env var name → JSON field name (container thresholds).
_CONTAINER_ENV_LOCKS = {
    "ROS_CONTAINER_CPU_COST_PERCENTILE": "cpu_cost_percentile",
    "ROS_CONTAINER_CPU_PERF_PERCENTILE": "cpu_perf_percentile",
    "ROS_CONTAINER_MEM_COST_PERCENTILE": "mem_cost_percentile",
    "ROS_CONTAINER_MEM_PERF_PERCENTILE": "mem_perf_percentile",
    "ROS_CONTAINER_MIN_MARGIN": "min_margin",
    "ROS_CONTAINER_MAX_MARGIN": "max_margin",
    "ROS_CONTAINER_LIMIT_MULTIPLIER": "limit_multiplier",
    "ROS_CONTAINER_CPU_FLOOR_MC": "cpu_floor_mc",
    "ROS_CONTAINER_IDLE_CPU_THRESHOLD_MC": "idle_cpu_threshold_mc",
    "ROS_CONTAINER_IDLE_MEM_THRESHOLD_KIB": "idle_mem_threshold_kib",
    "ROS_CONTAINER_MEM_TREND_SLOPE_THRESHOLD": "mem_trend_slope_threshold",
    "ROS_CONTAINER_LOW_CONFIDENCE_THRESHOLD": "low_confidence_threshold",
}


def _threshold_settings_base(ros_api_url: str, recommendation_type: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        f"recommendations/openshift/settings/{recommendation_type}"
    )


def _threshold_settings_deprecated_base(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/settings/thresholds"
    )


def _fresh_auth(
    keycloak_config, cluster_config, http_session: requests.Session
) -> dict[str, str]:
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.fail("Could not obtain JWT token")
    return auth


def _get_thresholds(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    recommendation_type: str,
) -> requests.Response:
    return session.get(
        _threshold_settings_base(ros_api_url, recommendation_type),
        headers=auth,
        timeout=30,
    )


def _put_thresholds(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    recommendation_type: str,
    body: dict[str, Any],
) -> requests.Response:
    return session.put(
        _threshold_settings_base(ros_api_url, recommendation_type),
        headers={**auth, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )


def _delete_thresholds(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    recommendation_type: str,
) -> requests.Response:
    return session.delete(
        _threshold_settings_base(ros_api_url, recommendation_type),
        headers=auth,
        timeout=60,
    )


def _ros_api_env(cluster_config) -> dict[str, str]:
    """Return ros-api container env as name → value."""
    result = run_oc_command([
        "get", "deployment", f"{cluster_config.helm_release_name}-ros-api",
        "-n", cluster_config.namespace,
        "-o", "json",
    ], check=False)
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    env_list = (
        data.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [{}])[0]
        .get("env", [])
    )
    return {
        item["name"]: item.get("value", "")
        for item in env_list
        if item.get("name")
    }


def _locked_container_fields_from_env(cluster_config) -> list[str]:
    env = _ros_api_env(cluster_config)
    return [
        field
        for env_name, field in _CONTAINER_ENV_LOCKS.items()
        if env_name in env
    ]


def _recommendation_fingerprint(item: dict[str, Any]) -> dict[str, Any]:
    """Capture comparable fields for async recalculation detection."""
    recs = item.get("recommendations") or {}
    term = (
        recs.get("medium_term")
        or recs.get("medium")
        or (recs.get("recommendation_terms") or {}).get("medium_term")
        or {}
    )
    if "cost" in term:
        engine = term["cost"]
    else:
        engine = (term.get("recommendation_engines") or {}).get("cost") or {}

    return {
        "id": item.get("id"),
        "last_reported": item.get("last_reported"),
        "cpu_request_millicores": engine.get("cpu_request_millicores"),
        "memory_request_kib": engine.get("memory_request_kib"),
        "estimated_monthly_savings": parse_savings_value(
            item.get("estimated_monthly_savings")
        ),
    }


def _fetch_first_recommendation(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
) -> Optional[dict[str, Any]]:
    resp = session.get(
        get_recommendations_endpoint(ros_api_url),
        headers=auth,
        params={"limit": 1},
        timeout=60,
    )
    if resp.status_code != 200:
        return None
    data = resp.json().get("data") or []
    return data[0] if data else None


@pytest.fixture
def threshold_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.integration
class TestThresholdSettingsE2E:
    """Threshold settings CRUD and async recalculation against a deployed cluster."""

    def test_threshold_get_defaults_container(
        self,
        ros_api_url: str,
        threshold_auth: dict,
        http_session: requests.Session,
    ):
        resp = _get_thresholds(http_session, ros_api_url, threshold_auth, "container")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for field in _TYPE_EXPECTED_FIELDS["container"]:
            assert field in body, f"missing {field!r} in container defaults"
        assert body["cpu_cost_percentile"] == pytest.approx(
            _CONTAINER_DEFAULTS["cpu_cost_percentile"], rel=1e-6
        )
        assert isinstance(body["locked_fields"], list)

    def test_threshold_get_defaults_all_types(
        self,
        ros_api_url: str,
        threshold_auth: dict,
        http_session: requests.Session,
    ):
        for rec_type in _RECOMMENDATION_TYPES:
            resp = _get_thresholds(http_session, ros_api_url, threshold_auth, rec_type)
            assert resp.status_code == 200, f"{rec_type}: {resp.text}"
            body = resp.json()
            for field in _TYPE_EXPECTED_FIELDS[rec_type]:
                assert field in body, f"{rec_type} missing {field!r}"

    def test_threshold_deprecated_alias_still_works(
        self,
        ros_api_url: str,
        threshold_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _threshold_settings_deprecated_base(ros_api_url),
            headers=threshold_auth,
            params={"recommendation_type": "container"},
            timeout=30,
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("Deprecation") == "true"
        link = resp.headers.get("Link", "")
        assert "/settings/container" in link
        assert 'rel="successor-version"' in link

    def test_threshold_deprecated_get_requires_recommendation_type(
        self,
        ros_api_url: str,
        threshold_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _threshold_settings_deprecated_base(ros_api_url),
            headers=threshold_auth,
            timeout=30,
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("status") == "error"
        assert "recommendation_type" in body.get("message", "").lower()

    def test_threshold_deprecated_get_invalid_type_returns_400(
        self,
        ros_api_url: str,
        threshold_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _threshold_settings_deprecated_base(ros_api_url),
            headers=threshold_auth,
            params={"recommendation_type": "invalid"},
            timeout=30,
        )
        assert resp.status_code == 400, resp.text
        assert "recommendation_type" in resp.json().get("message", "").lower()

    @pytest.mark.parametrize(
        "recommendation_type,custom,field,expected",
        _THRESHOLD_PUT_CASES,
    )
    def test_threshold_put_persists(
        self,
        recommendation_type: str,
        custom: dict[str, float],
        field: str,
        expected: float,
        ros_api_url: str,
        threshold_auth: dict,
        http_session: requests.Session,
        keycloak_config,
        cluster_config,
    ):
        try:
            put_resp = _put_thresholds(
                http_session, ros_api_url, threshold_auth, recommendation_type, custom
            )
            assert put_resp.status_code == 200, put_resp.text
            assert put_resp.json()[field] == pytest.approx(expected, rel=1e-6)

            get_resp = _get_thresholds(
                http_session, ros_api_url, threshold_auth, recommendation_type
            )
            assert get_resp.status_code == 200, get_resp.text
            assert get_resp.json()[field] == pytest.approx(expected, rel=1e-6)
        finally:
            auth = _fresh_auth(keycloak_config, cluster_config, http_session)
            _delete_thresholds(http_session, ros_api_url, auth, recommendation_type)

    def test_threshold_put_validation_rejects_out_of_range(
        self,
        ros_api_url: str,
        threshold_auth: dict,
        http_session: requests.Session,
    ):
        resp = _put_thresholds(
            http_session,
            ros_api_url,
            threshold_auth,
            "container",
            {"cpu_cost_percentile": 1.5},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("status") == "error"
        assert "validation_errors" in body
        assert body["validation_errors"]

    def test_threshold_put_validation_rejects_unknown_field(
        self,
        ros_api_url: str,
        threshold_auth: dict,
        http_session: requests.Session,
    ):
        resp = _put_thresholds(
            http_session,
            ros_api_url,
            threshold_auth,
            "container",
            {"not_a_valid_threshold_field": 0.99},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("status") == "error"
        assert "validation_errors" in body

    @pytest.mark.parametrize(
        "recommendation_type,custom,field,default_value",
        _THRESHOLD_DELETE_CASES,
    )
    def test_threshold_delete_resets_to_defaults(
        self,
        recommendation_type: str,
        custom: dict[str, float],
        field: str,
        default_value: float,
        ros_api_url: str,
        threshold_auth: dict,
        http_session: requests.Session,
        keycloak_config,
        cluster_config,
    ):
        try:
            put_resp = _put_thresholds(
                http_session,
                ros_api_url,
                threshold_auth,
                recommendation_type,
                custom,
            )
            assert put_resp.status_code == 200, put_resp.text

            del_resp = _delete_thresholds(
                http_session, ros_api_url, threshold_auth, recommendation_type
            )
            assert del_resp.status_code in (200, 204), del_resp.text

            get_resp = _get_thresholds(
                http_session, ros_api_url, threshold_auth, recommendation_type
            )
            assert get_resp.status_code == 200, get_resp.text
            assert get_resp.json()[field] == pytest.approx(default_value, rel=1e-6)
            if recommendation_type == "container":
                body = get_resp.json()
                assert body["cpu_cost_percentile"] == pytest.approx(
                    _CONTAINER_DEFAULTS["cpu_cost_percentile"], rel=1e-6
                )
        finally:
            auth = _fresh_auth(keycloak_config, cluster_config, http_session)
            _delete_thresholds(http_session, ros_api_url, auth, recommendation_type)

    @pytest.mark.timeout(120)
    def test_threshold_put_triggers_recalculation(
        self,
        ros_api_url: str,
        threshold_auth: dict,
        http_session: requests.Session,
        keycloak_config,
        cluster_config,
    ):
        baseline_item = _fetch_first_recommendation(
            http_session, ros_api_url, threshold_auth
        )
        if not baseline_item:
            pytest.skip("No container recommendations available for recalculation proof")

        baseline = _recommendation_fingerprint(baseline_item)
        rec_id = baseline.get("id")
        assert rec_id, "Recommendation item must include id"

        try:
            put_resp = _put_thresholds(
                http_session,
                ros_api_url,
                threshold_auth,
                "container",
                {"cpu_cost_percentile": 0.95},
            )
            assert put_resp.status_code == 200, put_resp.text

            def recalculation_observed() -> bool:
                auth = _fresh_auth(keycloak_config, cluster_config, http_session)
                resp = http_session.get(
                    get_recommendations_endpoint(ros_api_url),
                    headers=auth,
                    params={"limit": 50},
                    timeout=60,
                )
                if resp.status_code != 200:
                    return False
                for item in resp.json().get("data") or []:
                    if item.get("id") != rec_id:
                        continue
                    current = _recommendation_fingerprint(item)
                    if current.get("last_reported") != baseline.get("last_reported"):
                        return True
                    if current != baseline:
                        return True
                return False

            assert wait_for_condition(
                recalculation_observed,
                timeout=60,
                interval=5,
                description="recommendation updated after threshold PUT",
            ), (
                "Expected recommendation to change within 60s after threshold PUT "
                f"(baseline={baseline})"
            )
        finally:
            auth = _fresh_auth(keycloak_config, cluster_config, http_session)
            _delete_thresholds(http_session, ros_api_url, auth, "container")

    def test_threshold_locked_fields_when_env_set(
        self,
        ros_api_url: str,
        threshold_auth: dict,
        http_session: requests.Session,
        cluster_config,
    ):
        expected_locked = _locked_container_fields_from_env(cluster_config)
        if not expected_locked:
            pytest.skip(
                "No ROS_CONTAINER_* threshold env vars on ros-api deployment; "
                "set Values.ros.thresholdEnv in the chart to test locked_fields"
            )

        resp = _get_thresholds(http_session, ros_api_url, threshold_auth, "container")
        assert resp.status_code == 200, resp.text
        locked_fields = resp.json().get("locked_fields") or []
        assert locked_fields, "locked_fields should be populated when admin env overrides exist"
        for field in expected_locked:
            assert field in locked_fields, (
                f"expected {field!r} in locked_fields (from deployment env), got {locked_fields}"
            )

    def test_threshold_put_multiple_types_independent(
        self,
        ros_api_url: str,
        threshold_auth: dict,
        http_session: requests.Session,
        keycloak_config,
        cluster_config,
    ):
        try:
            put_resp = _put_thresholds(
                http_session,
                ros_api_url,
                threshold_auth,
                "container",
                {"cpu_cost_percentile": 0.68},
            )
            assert put_resp.status_code == 200, put_resp.text

            node_resp = _get_thresholds(http_session, ros_api_url, threshold_auth, "node")
            assert node_resp.status_code == 200, node_resp.text
            node_body = node_resp.json()
            assert node_body["cost_target_utilization"] == pytest.approx(
                _NODE_DEFAULTS["cost_target_utilization"], rel=1e-6
            )
            assert node_body["underutil_threshold"] == pytest.approx(
                _NODE_DEFAULTS["underutil_threshold"], rel=1e-6
            )
        finally:
            auth = _fresh_auth(keycloak_config, cluster_config, http_session)
            _delete_thresholds(http_session, ros_api_url, auth, "container")
