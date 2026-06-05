"""E2E tests for ROS PVC/storage recommendations."""

from __future__ import annotations

from typing import Any, Optional

import pytest
import requests

from utils import assert_structured_savings, parse_savings_value

from suites.ros.test_recommendations import get_fresh_token


def _pvcs_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/pvcs"
    )


def _pvc_detail_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/pvcs/detail"
    )


def _pvc_settings_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/settings/pvc"
    )


def _notification_codes_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/notification-codes"
    )


def _savings_summary_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/savings-summary"
    )


PVC_NOTIFICATION_CODES = frozenset({20, 25, 29, 30})

PVC_SETTINGS_THRESHOLD_FIELDS = frozenset(
    {
        "oversized_threshold",
        "near_full_threshold",
        "min_trend_days",
        "days_to_full_alert",
        "locked_fields",
    }
)


def _put_pvc_settings(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    body: dict[str, Any],
) -> requests.Response:
    return session.put(
        _pvc_settings_url(ros_api_url),
        headers={**auth, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )


def _delete_pvc_settings(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
) -> requests.Response:
    return session.delete(
        _pvc_settings_url(ros_api_url),
        headers=auth,
        timeout=60,
    )


def _fetch_pvcs(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    return session.get(
        _pvcs_url(ros_api_url),
        headers=auth,
        params=params or {},
        timeout=60,
    )


def _fetch_pvc_detail(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: dict[str, str],
) -> requests.Response:
    return session.get(
        _pvc_detail_url(ros_api_url),
        headers=auth,
        params=params,
        timeout=60,
    )


def _skip_if_no_pvc_plugin(resp: requests.Response) -> None:
    if resp.status_code == 404:
        pytest.skip("PVC recommendations plugin not enabled (404 on /pvcs)")


def _first_pvc_item(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    resp = _fetch_pvcs(session, ros_api_url, auth, params or {"limit": 5})
    _skip_if_no_pvc_plugin(resp)
    assert resp.status_code == 200, resp.text
    items = resp.json().get("data") or []
    if not items:
        pytest.skip("No PVC recommendation data in cluster")
    return items[0]


@pytest.fixture
def pvc_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestPVCRecommendationsE2E:
    """PVC recommendation list API."""

    def test_pvc_list_returns_200(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_pvcs(http_session, ros_api_url, pvc_auth, {"limit": 10})
        if resp.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled (404 on /pvcs)")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert isinstance(body["data"], list)

    def test_pvc_list_data_fields(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_pvcs(http_session, ros_api_url, pvc_auth, {"limit": 5})
        if resp.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No PVC recommendation data in cluster")

        item = body["data"][0]
        assert "namespace" in item
        assert "persistentvolumeclaim" in item
        assert "capacity_bytes" in item
        assert isinstance(item["capacity_bytes"], int)
        assert item["capacity_bytes"] >= 0
        # vm_name is optional; when present it must be a non-empty string (operator storage CSV).
        if "vm_name" in item and item["vm_name"]:
            assert isinstance(item["vm_name"], str)

    def test_pvc_filter_by_cluster(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_pvcs(http_session, ros_api_url, pvc_auth, {"limit": 5})
        if baseline.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled")
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No PVC recommendation data in cluster")

        cluster_uuid = items[0].get("cluster_uuid")
        assert cluster_uuid, "PVC item must include cluster_uuid"

        filtered = _fetch_pvcs(
            http_session,
            ros_api_url,
            pvc_auth,
            {"cluster_uuid": cluster_uuid, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for item in filtered.json().get("data") or []:
            assert item.get("cluster_uuid") == cluster_uuid

    def test_pvc_filter_by_namespace(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_pvcs(http_session, ros_api_url, pvc_auth, {"limit": 5})
        if baseline.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled")
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No PVC recommendation data in cluster")

        namespace = items[0].get("namespace")
        assert namespace, "PVC item must include namespace"

        filtered = _fetch_pvcs(
            http_session,
            ros_api_url,
            pvc_auth,
            {"namespace": namespace, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for item in filtered.json().get("data") or []:
            assert item.get("namespace") == namespace

    def test_pvc_savings_is_numeric(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_pvcs(http_session, ros_api_url, pvc_auth, {"limit": 20})
        if resp.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No PVC recommendation data in cluster")

        saw_savings = False
        for item in items:
            savings_obj = item.get("estimated_monthly_savings")
            if savings_obj is None:
                continue
            saw_savings = True
            assert_structured_savings(savings_obj)
            savings = parse_savings_value(savings_obj)
            assert savings is not None and isinstance(savings, (int, float)), (
                f"non-numeric savings on PVC {item.get('persistentvolumeclaim')}: {savings!r}"
            )
        if not saw_savings:
            # Savings may be absent/null for non-actionable PVC rows; field presence is optional.
            assert "estimated_monthly_savings" in items[0]

    def test_pvc_pagination(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        first = _fetch_pvcs(
            http_session, ros_api_url, pvc_auth, {"limit": 2, "offset": 0}
        )
        if first.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled")
        assert first.status_code == 200, first.text
        total = first.json().get("meta", {}).get("count", 0)
        if total == 0:
            pytest.skip("No PVC recommendation data in cluster")
        if total <= 2:
            pytest.skip("Need more than two PVC recommendations for pagination")

        second = _fetch_pvcs(
            http_session, ros_api_url, pvc_auth, {"limit": 2, "offset": 2}
        )
        assert second.status_code == 200, second.text
        page1_keys = {
            (i["cluster_uuid"], i["namespace"], i["persistentvolumeclaim"])
            for i in first.json()["data"]
        }
        page2_keys = {
            (i["cluster_uuid"], i["namespace"], i["persistentvolumeclaim"])
            for i in second.json()["data"]
        }
        assert page1_keys.isdisjoint(page2_keys)

    def test_pvc_detail_returns_200_with_terms(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        item = _first_pvc_item(http_session, ros_api_url, pvc_auth)
        detail = _fetch_pvc_detail(
            http_session,
            ros_api_url,
            pvc_auth,
            {
                "cluster_uuid": item["cluster_uuid"],
                "namespace": item["namespace"],
                "persistentvolumeclaim": item["persistentvolumeclaim"],
            },
        )
        if detail.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled (404 on /pvcs/detail)")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["cluster_uuid"] == item["cluster_uuid"]
        assert body["namespace"] == item["namespace"]
        assert body["persistentvolumeclaim"] == item["persistentvolumeclaim"]
        assert "terms" in body
        assert isinstance(body["terms"], dict)
        assert body["terms"], "detail response must include at least one term"
        for term_name, term_row in body["terms"].items():
            assert term_name in ("short", "medium", "long")
            assert term_row.get("recommendation_type")
            assert "usage_ratio" in term_row
            assert "capacity_bytes" in term_row

    def test_pvc_vm_name_from_rightsizing_fixture(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        """pvc-vm-disk in pvc-rightsizing should expose vm_name after operator/nise ingest."""
        resp = _fetch_pvcs(
            http_session,
            ros_api_url,
            pvc_auth,
            {"namespace": "pvc-rightsizing", "limit": 50},
        )
        if resp.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        vm_rows = [
            item
            for item in resp.json().get("data") or []
            if item.get("persistentvolumeclaim") == "pvc-vm-disk"
        ]
        if not vm_rows:
            pytest.skip("pvc-vm-disk not in recommendation set (ingest may be pending)")
        assert vm_rows[0].get("vm_name") == "fedora-vm", vm_rows[0]

    def test_pvc_filter_by_storageclass(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        item = _first_pvc_item(http_session, ros_api_url, pvc_auth)
        storageclass = item.get("storageclass")
        if not storageclass:
            pytest.skip("PVC row has no storageclass — cannot verify filter[storageclass]")

        filtered = _fetch_pvcs(
            http_session,
            ros_api_url,
            pvc_auth,
            {"filter[storageclass]": storageclass, "limit": 50},
        )
        _skip_if_no_pvc_plugin(filtered)
        assert filtered.status_code == 200, filtered.text
        for row in filtered.json().get("data") or []:
            assert row.get("storageclass") == storageclass

    def test_pvc_filter_tag(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_pvcs(http_session, ros_api_url, pvc_auth, {"limit": 5})
        _skip_if_no_pvc_plugin(baseline)
        assert baseline.status_code == 200, baseline.text
        unfiltered_count = baseline.json().get("meta", {}).get("count", 0)
        if unfiltered_count == 0:
            pytest.skip("No PVC recommendation data in cluster")

        resp = _fetch_pvcs(
            http_session,
            ros_api_url,
            pvc_auth,
            {"filter[tag:environment]": "production", "limit": 10},
        )
        _skip_if_no_pvc_plugin(resp)
        if resp.status_code == 400:
            pytest.skip("Tag filtering not enabled or invalid tag key")
        assert resp.status_code == 200, resp.text
        filtered = resp.json()
        filtered_count = filtered.get("meta", {}).get("count", 0)
        assert filtered_count <= unfiltered_count
        if unfiltered_count > 0 and filtered_count == unfiltered_count:
            pytest.skip("Tag filter did not narrow results; no matching tagged PVC namespaces")
        if filtered_count == 0:
            pytest.skip("No PVCs match filter[tag:environment]=production")

    def test_pvc_filter_by_term(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        _first_pvc_item(http_session, ros_api_url, pvc_auth)

        counts: dict[str, int] = {}
        for term in ("short", "medium", "long"):
            resp = _fetch_pvcs(
                http_session,
                ros_api_url,
                pvc_auth,
                {"filter[term]": term, "limit": 50},
            )
            _skip_if_no_pvc_plugin(resp)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            counts[term] = body.get("meta", {}).get("count", 0)
            for row in body.get("data") or []:
                assert row.get("term") == term, (
                    f"filter[term]={term} returned row with term={row.get('term')!r}"
                )

        if sum(counts.values()) == 0:
            pytest.skip("No PVC rows for any term filter")

    def test_pvc_filter_by_term_short_term_alias(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        """filter[term]=short_term must resolve to DB term short."""
        _first_pvc_item(http_session, ros_api_url, pvc_auth)

        resp = _fetch_pvcs(
            http_session,
            ros_api_url,
            pvc_auth,
            {"filter[term]": "short_term", "limit": 50},
        )
        _skip_if_no_pvc_plugin(resp)
        assert resp.status_code == 200, resp.text
        for row in resp.json().get("data") or []:
            assert row.get("term") == "short", (
                f"filter[term]=short_term returned row with term={row.get('term')!r}"
            )

    def test_pvc_csv_export(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_pvcs(
            http_session,
            ros_api_url,
            pvc_auth,
            {"format": "csv", "limit": 100},
        )
        _skip_if_no_pvc_plugin(resp)
        assert resp.status_code == 200, resp.text
        content_type = resp.headers.get("Content-Type", "")
        assert "text/csv" in content_type, content_type
        body = resp.text
        assert "cluster_uuid" in body
        assert "persistentvolumeclaim" in body
        assert "recommendation_type" in body

    def test_pvc_detail_historical_usage(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        item = _first_pvc_item(http_session, ros_api_url, pvc_auth)
        detail = _fetch_pvc_detail(
            http_session,
            ros_api_url,
            pvc_auth,
            {
                "cluster_uuid": item["cluster_uuid"],
                "namespace": item["namespace"],
                "persistentvolumeclaim": item["persistentvolumeclaim"],
            },
        )
        if detail.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled (404 on /pvcs/detail)")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert "terms" in body
        history = body.get("historical_usage")
        if not history:
            pytest.skip("No historical_usage points for PVC in environment")
        assert isinstance(history, list)
        point = history[0]
        assert "date" in point
        assert "usage_bytes_max" in point

    def test_pvc_orphaned_idle_fields(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_pvcs(
            http_session,
            ros_api_url,
            pvc_auth,
            {
                "filter[recommendation_type]": "orphaned",
                "filter[term]": "medium",
                "limit": 50,
            },
        )
        _skip_if_no_pvc_plugin(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []
        if not rows:
            pytest.skip("No orphaned PVC recommendations in cluster")

        orphaned = rows[0]
        assert orphaned.get("recommendation_type") == "orphaned"
        assert orphaned.get("idle_since"), "orphaned PVC must include idle_since"
        idle_days = orphaned.get("idle_duration_days")
        assert idle_days is not None and idle_days > 0

    def test_pvc_orphaned_savings_positive(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_pvcs(
            http_session,
            ros_api_url,
            pvc_auth,
            {
                "filter[recommendation_type]": "orphaned",
                "filter[term]": "medium",
                "limit": 50,
            },
        )
        _skip_if_no_pvc_plugin(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []
        if not rows:
            pytest.skip("No orphaned PVC recommendations in cluster")

        rows_with_savings = [
            row
            for row in rows
            if row.get("estimated_monthly_savings") is not None
        ]
        if not rows_with_savings:
            pytest.skip("No orphaned PVC rows with estimated_monthly_savings populated")

        assert any(
            parse_savings_value(row["estimated_monthly_savings"]) > 0
            for row in rows_with_savings
            if parse_savings_value(row["estimated_monthly_savings"]) is not None
        ), "expected at least one orphaned PVC with positive estimated_monthly_savings"

    @pytest.mark.component
    def test_pvc_settings_get(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _pvc_settings_url(ros_api_url),
            headers=pvc_auth,
            timeout=60,
        )
        if resp.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        missing = PVC_SETTINGS_THRESHOLD_FIELDS - set(body.keys())
        assert not missing, f"settings/pvc missing fields: {missing}"
        assert isinstance(body["locked_fields"], list)

    @pytest.mark.component
    def test_pvc_settings_put_roundtrip(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
        keycloak_config,
        cluster_config,
    ):
        get_resp = http_session.get(
            _pvc_settings_url(ros_api_url),
            headers=pvc_auth,
            timeout=60,
        )
        if get_resp.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled")
        assert get_resp.status_code == 200, get_resp.text
        baseline = get_resp.json()
        locked = frozenset(baseline.get("locked_fields") or [])
        if "oversized_threshold" in locked:
            pytest.skip("oversized_threshold is env-locked on this deployment")

        custom_threshold = 0.5
        if baseline.get("oversized_threshold") == custom_threshold:
            custom_threshold = 0.45

        try:
            put_resp = _put_pvc_settings(
                http_session,
                ros_api_url,
                pvc_auth,
                {"oversized_threshold": custom_threshold},
            )
            assert put_resp.status_code == 200, put_resp.text
            assert put_resp.json()["oversized_threshold"] == pytest.approx(
                custom_threshold, rel=1e-6
            )

            verify_resp = http_session.get(
                _pvc_settings_url(ros_api_url),
                headers=pvc_auth,
                timeout=60,
            )
            assert verify_resp.status_code == 200, verify_resp.text
            assert verify_resp.json()["oversized_threshold"] == pytest.approx(
                custom_threshold, rel=1e-6
            )
        finally:
            auth = get_fresh_token(keycloak_config, cluster_config, http_session)
            if auth:
                _delete_pvc_settings(http_session, ros_api_url, auth)

    @pytest.mark.component
    def test_pvc_notification_codes_catalog(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_pvcs(http_session, ros_api_url, pvc_auth, {"limit": 1})
        _skip_if_no_pvc_plugin(baseline)

        resp = http_session.get(
            _notification_codes_url(ros_api_url),
            headers=pvc_auth,
            params={"filter[plugin]": "pvc"},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        codes = {entry["code"] for entry in resp.json().get("data") or []}
        assert PVC_NOTIFICATION_CODES.issubset(codes), (
            f"expected PVC codes {PVC_NOTIFICATION_CODES}, got {codes}"
        )

    @pytest.mark.component
    def test_pvc_growth_fields_on_near_full(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_pvcs(
            http_session,
            ros_api_url,
            pvc_auth,
            {
                "namespace": PVC_RIGHTSIZING_NAMESPACE,
                "filter[recommendation_type]": "near_full",
                "filter[term]": "medium",
                "limit": 50,
            },
        )
        _skip_if_no_pvc_plugin(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []
        near_full = next(
            (
                row
                for row in rows
                if row.get("persistentvolumeclaim") == "pvc-near-full"
            ),
            None,
        )
        if near_full is None:
            pytest.skip(
                "pvc-near-full not in near_full results "
                f"(namespace {PVC_RIGHTSIZING_NAMESPACE}; ingest may be pending)"
            )
        growth = near_full.get("growth_bytes_per_day")
        if growth is None or growth == 0:
            pytest.skip(
                "pvc-near-full has no growth projection in environment "
                "(static NISE fixture uses flat full_period usage)"
            )
        assert near_full.get("days_to_full") is not None

    @pytest.mark.component
    def test_pvc_fleet_savings_rollup(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        list_resp = _fetch_pvcs(http_session, ros_api_url, pvc_auth, {"limit": 5})
        _skip_if_no_pvc_plugin(list_resp)
        if list_resp.json().get("meta", {}).get("count", 0) == 0:
            pytest.skip("No PVC recommendation data in cluster")

        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=pvc_auth,
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        by_plugin = resp.json().get("by_plugin") or {}
        assert "pvc" in by_plugin, f"by_plugin missing pvc key: {by_plugin}"
        assert isinstance(by_plugin["pvc"], (int, float))


# Deterministic PVC names from tests/data/nise_templates/ocp_report_pvc_rightsizing.yml
PVC_RIGHTSIZING_NAMESPACE = "pvc-rightsizing"
PVC_FIXTURE_CLASSIFICATIONS = {
    "pvc-oversized": "oversized",
    "pvc-near-full": "near_full",
    "pvc-orphaned": "orphaned",
    "pvc-healthy": "healthy",
}


@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestPVCRightsizingFixtureClassifications:
    """Assert filter[recommendation_type] against nise ocp_report_pvc_rightsizing.yml PVCs."""

    @pytest.mark.parametrize(
        ("pvc_name", "expected_type"),
        list(PVC_FIXTURE_CLASSIFICATIONS.items()),
    )
    def test_pvc_filter_recommendation_type_matches_fixture(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
        pvc_name: str,
        expected_type: str,
    ):
        filtered = _fetch_pvcs(
            http_session,
            ros_api_url,
            pvc_auth,
            {
                "namespace": PVC_RIGHTSIZING_NAMESPACE,
                "filter[recommendation_type]": expected_type,
                "filter[term]": "medium",
                "limit": 50,
            },
        )
        _skip_if_no_pvc_plugin(filtered)
        assert filtered.status_code == 200, filtered.text
        rows = filtered.json().get("data") or []
        matching = [
            row
            for row in rows
            if row.get("persistentvolumeclaim") == pvc_name
            and row.get("namespace") == PVC_RIGHTSIZING_NAMESPACE
        ]
        if not matching:
            pytest.skip(
                f"{pvc_name} not in {expected_type} results "
                f"(namespace {PVC_RIGHTSIZING_NAMESPACE}; ingest may be pending)"
            )
        assert matching[0].get("recommendation_type") == expected_type
        for row in rows:
            assert row.get("recommendation_type") == expected_type
