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

    def test_cluster_quota_settings_put_persists(
        self,
        ros_api_url: str,
        cluster_quota_settings_auth: dict,
        http_session: requests.Session,
        keycloak_config,
        cluster_config,
    ):
        custom = {
            "headroom_percent": 15,
            "high_risk_threshold_percent": 85,
            "medium_risk_threshold_percent": 65,
        }
        try:
            put_resp = _put_settings(
                http_session,
                ros_api_url,
                cluster_quota_settings_auth,
                custom,
            )
            _skip_if_plugin_disabled(put_resp)
            assert put_resp.status_code == 200, put_resp.text
            put_body = put_resp.json()
            assert put_body["headroom_percent"] == 15
            assert put_body["high_risk_threshold_percent"] == 85
            assert put_body["medium_risk_threshold_percent"] == 65

            get_resp = _get_settings(
                http_session, ros_api_url, cluster_quota_settings_auth
            )
            assert get_resp.status_code == 200, get_resp.text
            get_body = get_resp.json()
            assert get_body["headroom_percent"] == 15
            assert get_body["medium_risk_threshold_percent"] == 65
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
        put_resp = _put_settings(
            http_session,
            ros_api_url,
            cluster_quota_settings_auth,
            {
                "headroom_percent": 20,
                "high_risk_threshold_percent": 80,
                "medium_risk_threshold_percent": 55,
            },
        )
        _skip_if_plugin_disabled(put_resp)
        assert put_resp.status_code == 200, put_resp.text

        del_resp = _delete_settings(
            http_session, ros_api_url, cluster_quota_settings_auth
        )
        assert del_resp.status_code == 200, del_resp.text
        del_body = del_resp.json()
        for field, expected in _DEFAULTS.items():
            assert del_body[field] == expected

        get_resp = _get_settings(
            http_session, ros_api_url, cluster_quota_settings_auth
        )
        assert get_resp.status_code == 200, get_resp.text
        get_body = get_resp.json()
        for field, expected in _DEFAULTS.items():
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
