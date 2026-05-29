"""E2E tests for ROS ClusterResourceQuota settings API.

Endpoints:
  GET/PUT/DELETE /api/cost-management/v1/recommendations/openshift/settings/cluster-quota
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token

_DEFAULTS = {
    "headroom_percent": 10,
    "high_risk_threshold_percent": 90,
    "medium_risk_threshold_percent": 70,
}
_ALL_SETTINGS_FIELDS = frozenset(_DEFAULTS.keys())


def _cluster_quota_settings_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/settings/cluster-quota"
    )


def _skip_if_plugin_disabled(resp: requests.Response) -> None:
    if resp.status_code == 404:
        pytest.skip(
            "Cluster quota recommendations plugin not enabled "
            "(404 on /settings/cluster-quota)"
        )


def _fetch_locked_fields(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
) -> list[str]:
    resp = _get_settings(session, ros_api_url, auth)
    _skip_if_plugin_disabled(resp)
    assert resp.status_code == 200, resp.text
    return resp.json().get("locked_fields") or []


def _skip_if_all_fields_locked(locked_fields: list[str]) -> None:
    if locked_fields and _ALL_SETTINGS_FIELDS <= frozenset(locked_fields):
        pytest.skip("All settings fields locked by env vars on this deployment")


def _filter_unlocked_payload(
    payload: dict[str, Any], locked_fields: list[str]
) -> dict[str, Any]:
    locked = frozenset(locked_fields)
    return {key: value for key, value in payload.items() if key not in locked}


def _get_settings(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
) -> requests.Response:
    return session.get(
        _cluster_quota_settings_url(ros_api_url),
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
        _cluster_quota_settings_url(ros_api_url),
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
        _cluster_quota_settings_url(ros_api_url),
        headers=auth,
        timeout=60,
    )


@pytest.fixture
def cluster_quota_settings_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.integration
class TestClusterQuotaSettingsE2E:
    """ClusterResourceQuota threshold settings CRUD against a deployed cluster."""

    def test_cluster_quota_settings_get_defaults(
        self,
        ros_api_url: str,
        cluster_quota_settings_auth: dict,
        http_session: requests.Session,
    ):
        resp = _get_settings(http_session, ros_api_url, cluster_quota_settings_auth)
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for field, expected in _DEFAULTS.items():
            assert field in body, f"missing {field!r} in cluster-quota settings"
            assert body[field] == expected
        assert isinstance(body.get("locked_fields"), list)

    def test_cluster_quota_settings_locked_fields_returns_403(
        self,
        ros_api_url: str,
        cluster_quota_settings_auth: dict,
        http_session: requests.Session,
    ):
        """PUT on env-locked fields returns 403 with locked_fields (standard Helm sets ROS_CLUSTER_QUOTA_*)."""
        get_resp = _get_settings(
            http_session, ros_api_url, cluster_quota_settings_auth
        )
        _skip_if_plugin_disabled(get_resp)
        assert get_resp.status_code == 200, get_resp.text
        locked_fields = get_resp.json().get("locked_fields") or []
        if not locked_fields:
            pytest.skip("No locked fields — env vars not set")

        locked_field = locked_fields[0]
        alternate_values = {
            "headroom_percent": 20,
            "high_risk_threshold_percent": 85,
            "medium_risk_threshold_percent": 65,
        }
        put_payload = {**_DEFAULTS, locked_field: alternate_values[locked_field]}

        put_resp = _put_settings(
            http_session,
            ros_api_url,
            cluster_quota_settings_auth,
            put_payload,
        )
        _skip_if_plugin_disabled(put_resp)
        assert put_resp.status_code == 403, put_resp.text
        put_body = put_resp.json()
        assert put_body.get("status") == "error"
        resp_locked = put_body.get("locked_fields") or []
        assert locked_field in resp_locked
        for field in resp_locked:
            assert field in locked_fields

    def test_cluster_quota_settings_put_persists(
        self,
        ros_api_url: str,
        cluster_quota_settings_auth: dict,
        http_session: requests.Session,
        keycloak_config,
        cluster_config,
    ):
        locked_fields = _fetch_locked_fields(
            http_session, ros_api_url, cluster_quota_settings_auth
        )
        _skip_if_all_fields_locked(locked_fields)

        custom = {
            "headroom_percent": 15,
            "high_risk_threshold_percent": 85,
            "medium_risk_threshold_percent": 65,
        }
        unlocked_custom = _filter_unlocked_payload(custom, locked_fields)
        if not unlocked_custom:
            pytest.skip("All settings fields locked by env vars on this deployment")

        try:
            put_resp = _put_settings(
                http_session,
                ros_api_url,
                cluster_quota_settings_auth,
                unlocked_custom,
            )
            _skip_if_plugin_disabled(put_resp)
            assert put_resp.status_code == 200, put_resp.text
            put_body = put_resp.json()
            for field, value in unlocked_custom.items():
                assert put_body[field] == value

            get_resp = _get_settings(
                http_session, ros_api_url, cluster_quota_settings_auth
            )
            assert get_resp.status_code == 200, get_resp.text
            get_body = get_resp.json()
            for field, value in unlocked_custom.items():
                assert get_body[field] == value
        finally:
            auth = get_fresh_token(keycloak_config, cluster_config, http_session)
            if auth:
                _delete_settings(http_session, ros_api_url, auth)

    def test_cluster_quota_settings_delete_resets_defaults(
        self,
        ros_api_url: str,
        cluster_quota_settings_auth: dict,
        http_session: requests.Session,
    ):
        locked_fields = _fetch_locked_fields(
            http_session, ros_api_url, cluster_quota_settings_auth
        )
        _skip_if_all_fields_locked(locked_fields)

        custom = {
            "headroom_percent": 20,
            "high_risk_threshold_percent": 80,
            "medium_risk_threshold_percent": 55,
        }
        unlocked_custom = _filter_unlocked_payload(custom, locked_fields)
        if not unlocked_custom:
            pytest.skip("All settings fields locked by env vars on this deployment")

        put_resp = _put_settings(
            http_session,
            ros_api_url,
            cluster_quota_settings_auth,
            unlocked_custom,
        )
        _skip_if_plugin_disabled(put_resp)
        assert put_resp.status_code == 200, put_resp.text

        del_resp = _delete_settings(
            http_session, ros_api_url, cluster_quota_settings_auth
        )
        assert del_resp.status_code == 200, del_resp.text
        del_body = del_resp.json()
        locked = frozenset(locked_fields)
        for field, expected in _DEFAULTS.items():
            if field in locked:
                continue
            assert del_body[field] == expected

        get_resp = _get_settings(
            http_session, ros_api_url, cluster_quota_settings_auth
        )
        assert get_resp.status_code == 200, get_resp.text
        get_body = get_resp.json()
        for field, expected in _DEFAULTS.items():
            if field in locked:
                continue
            assert get_body[field] == expected

    def test_cluster_quota_settings_put_validation_headroom_over_100(
        self,
        ros_api_url: str,
        cluster_quota_settings_auth: dict,
        http_session: requests.Session,
    ):
        resp = _put_settings(
            http_session,
            ros_api_url,
            cluster_quota_settings_auth,
            {
                "headroom_percent": 150,
                "high_risk_threshold_percent": 90,
                "medium_risk_threshold_percent": 70,
            },
        )
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("status") == "error"
        assert body.get("validation_errors")

    def test_cluster_quota_settings_put_validation_medium_gte_high(
        self,
        ros_api_url: str,
        cluster_quota_settings_auth: dict,
        http_session: requests.Session,
    ):
        resp = _put_settings(
            http_session,
            ros_api_url,
            cluster_quota_settings_auth,
            {
                "headroom_percent": 10,
                "high_risk_threshold_percent": 70,
                "medium_risk_threshold_percent": 80,
            },
        )
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("status") == "error"
        assert body.get("validation_errors")
