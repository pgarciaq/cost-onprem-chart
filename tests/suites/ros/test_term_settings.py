"""E2E tests for ROS recommendation term settings API.

Endpoints:
  GET/PUT/DELETE /api/cost-management/v1/recommendations/openshift/settings/terms
  ?recommendation_type=<plugin>
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token

_TERM_NAMES = ("short", "medium", "long")

_DEFAULT_TERMS_BY_NAME = {
    "short": {"window_days": 1, "min_data_days": 1},
    "medium": {"window_days": 7, "min_data_days": 3},
    "long": {"window_days": 15, "min_data_days": 7},
}

_CUSTOM_TERMS_PAYLOAD = {
    "terms": [
        {"name": "short", "window_days": 3, "min_data_days": 2},
        {"name": "medium", "window_days": 14, "min_data_days": 7},
        {"name": "long", "window_days": 30, "min_data_days": 15},
    ]
}


def _terms_settings_url(ros_api_url: str, recommendation_type: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        f"recommendations/openshift/settings/terms"
    )


def _get_terms(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    recommendation_type: str,
) -> requests.Response:
    return session.get(
        _terms_settings_url(ros_api_url, recommendation_type),
        headers=auth,
        params={"recommendation_type": recommendation_type},
        timeout=30,
    )


def _put_terms(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    recommendation_type: str,
    body: dict[str, Any],
) -> requests.Response:
    return session.put(
        _terms_settings_url(ros_api_url, recommendation_type),
        headers={**auth, "Content-Type": "application/json"},
        params={"recommendation_type": recommendation_type},
        json=body,
        timeout=60,
    )


def _delete_terms(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    recommendation_type: str,
) -> requests.Response:
    return session.delete(
        _terms_settings_url(ros_api_url, recommendation_type),
        headers=auth,
        params={"recommendation_type": recommendation_type},
        timeout=60,
    )


def _terms_by_name(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    terms_list = body.get("terms") or []
    return {item["name"]: item for item in terms_list if item.get("name")}


def _assert_default_terms(body: dict[str, Any], recommendation_type: str) -> None:
    assert body.get("recommendation_type") == recommendation_type
    by_name = _terms_by_name(body)
    for name in _TERM_NAMES:
        assert name in by_name, f"missing term {name!r}"
        term = by_name[name]
        expected = _DEFAULT_TERMS_BY_NAME[name]
        assert term["window_days"] == expected["window_days"]
        assert term["min_data_days"] == expected["min_data_days"]
        assert "decay_halflife_hours" in term
        assert "locked" in term
        assert "is_default" in term


@pytest.fixture
def term_settings_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.component
class TestTermSettingsE2E:
    """Term settings CRUD for container and node recommendation types."""

    def test_term_settings_get_defaults_container(
        self,
        ros_api_url: str,
        term_settings_auth: dict,
        http_session: requests.Session,
    ):
        resp = _get_terms(http_session, ros_api_url, term_settings_auth, "container")
        assert resp.status_code == 200, resp.text
        _assert_default_terms(resp.json(), "container")

    def test_term_settings_get_defaults_node(
        self,
        ros_api_url: str,
        term_settings_auth: dict,
        http_session: requests.Session,
    ):
        resp = _get_terms(http_session, ros_api_url, term_settings_auth, "node")
        assert resp.status_code == 200, resp.text
        _assert_default_terms(resp.json(), "node")

    def test_term_settings_put_persists_container(
        self,
        ros_api_url: str,
        term_settings_auth: dict,
        http_session: requests.Session,
        keycloak_config,
        cluster_config,
    ):
        try:
            put_resp = _put_terms(
                http_session,
                ros_api_url,
                term_settings_auth,
                "container",
                _CUSTOM_TERMS_PAYLOAD,
            )
            assert put_resp.status_code == 200, put_resp.text
            put_body = put_resp.json()
            assert put_body.get("recommendation_type") == "container"
            by_name = _terms_by_name(put_body)
            assert by_name["short"]["window_days"] == 3
            assert by_name["medium"]["window_days"] == 14
            assert by_name["long"]["window_days"] == 30

            get_resp = _get_terms(
                http_session, ros_api_url, term_settings_auth, "container"
            )
            assert get_resp.status_code == 200, get_resp.text
            get_by_name = _terms_by_name(get_resp.json())
            assert get_by_name["short"]["window_days"] == 3
            assert get_by_name["medium"]["window_days"] == 14
            assert get_by_name["long"]["window_days"] == 30
        finally:
            auth = get_fresh_token(keycloak_config, cluster_config, http_session)
            if auth:
                _delete_terms(http_session, ros_api_url, auth, "container")

    def test_term_settings_delete_resets_defaults(
        self,
        ros_api_url: str,
        term_settings_auth: dict,
        http_session: requests.Session,
        keycloak_config,
        cluster_config,
    ):
        put_resp = _put_terms(
            http_session,
            ros_api_url,
            term_settings_auth,
            "container",
            _CUSTOM_TERMS_PAYLOAD,
        )
        assert put_resp.status_code == 200, put_resp.text

        del_resp = _delete_terms(
            http_session, ros_api_url, term_settings_auth, "container"
        )
        assert del_resp.status_code == 200, del_resp.text
        _assert_default_terms(del_resp.json(), "container")

        get_resp = _get_terms(
            http_session, ros_api_url, term_settings_auth, "container"
        )
        assert get_resp.status_code == 200, get_resp.text
        _assert_default_terms(get_resp.json(), "container")

    def test_term_settings_put_validation_invalid_window_days(
        self,
        ros_api_url: str,
        term_settings_auth: dict,
        http_session: requests.Session,
    ):
        resp = _put_terms(
            http_session,
            ros_api_url,
            term_settings_auth,
            "container",
            {
                "terms": [
                    {"name": "short", "window_days": 500, "min_data_days": 1},
                ]
            },
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("status") == "error"
        assert "window_days" in body.get("message", "").lower()

    def test_term_settings_put_validation_invalid_term_name(
        self,
        ros_api_url: str,
        term_settings_auth: dict,
        http_session: requests.Session,
    ):
        resp = _put_terms(
            http_session,
            ros_api_url,
            term_settings_auth,
            "container",
            {
                "terms": [
                    {"name": "invalid", "window_days": 7},
                ]
            },
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("status") == "error"

    def test_term_settings_get_requires_recommendation_type(
        self,
        ros_api_url: str,
        term_settings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _terms_settings_url(ros_api_url, "container"),
            headers=term_settings_auth,
            timeout=30,
        )
        assert resp.status_code == 400, resp.text
        assert "recommendation_type" in resp.json().get("message", "").lower()
