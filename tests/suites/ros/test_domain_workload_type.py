"""Domain (CRD) workload_type E2E tests.

Validates that arbitrary workload_type strings (e.g., "domain" for WebLogic
CRDs) flow through the NISE → ingestion → ROS pipeline and appear correctly
in the recommendations API, including filter and exclude query params.

Covers: COST-7274

Prerequisites:
  The ``seed_domain.yml`` NISE template must have been ingested into the
  cluster. Tests skip gracefully when domain data is absent.

Run:
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh -k TestDomainWorkloadType
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token, get_recommendations_endpoint


def _assert_paginated_envelope(body: dict[str, Any]) -> None:
    """Verify the response matches the standard paginated envelope."""
    assert "meta" in body, "response must include meta"
    assert "data" in body, "response must include data"
    assert body["data"] is None or isinstance(body["data"], list), (
        f"data must be a list or null, got {type(body['data'])}"
    )
    meta = body["meta"]
    assert isinstance(meta, dict)
    assert "count" in meta


@pytest.fixture
def domain_auth(keycloak_config, cluster_config, http_session):
    """Fresh JWT auth header for domain workload tests."""
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


def _fetch_recommendations(
    ros_api_url: str,
    auth: dict[str, str],
    http_session: requests.Session,
    params: dict[str, Any],
) -> dict[str, Any]:
    """GET recommendations/openshift/ with given params, assert 200."""
    endpoint = get_recommendations_endpoint(ros_api_url)
    resp = http_session.get(endpoint, headers=auth, params=params, timeout=60)
    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    _assert_paginated_envelope(body)
    return body


@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestDomainWorkloadType:
    """Validate domain workload_type support in ROS container recommendations."""

    def test_domain_workload_filter_accepted(
        self,
        ros_api_url: str,
        domain_auth: dict,
        http_session: requests.Session,
    ):
        """filter[workload_type]=domain returns 200 (not 400), proving the API
        accepts arbitrary workload_type strings beyond the legacy enum.

        Covers: COST-7274 — workload_type text migration.
        """
        body = _fetch_recommendations(
            ros_api_url, domain_auth, http_session,
            params={"filter[workload_type]": "domain", "limit": 10},
        )
        items = body.get("data") or []
        for item in items:
            wt = (item.get("workload_type") or "").lower()
            assert wt == "domain", (
                f"Filtered row has workload_type={item.get('workload_type')!r}, expected 'domain'"
            )

    def test_domain_workload_recommendations_exist(
        self,
        ros_api_url: str,
        domain_auth: dict,
        http_session: requests.Session,
    ):
        """After ingesting seed_domain.yml data, at least one container
        recommendation should exist with workload_type=domain.

        Skips if domain data has not been seeded into the cluster.
        """
        body = _fetch_recommendations(
            ros_api_url, domain_auth, http_session,
            params={"filter[workload_type]": "domain", "limit": 50},
        )
        items = body.get("data") or []
        if not items:
            pytest.skip(
                "No domain workload recommendations found — "
                "seed_domain.yml data may not have been ingested"
            )

        count = body.get("meta", {}).get("count", 0)
        assert count > 0, "meta.count should be > 0 when data items are returned"

        workload_types = {(item.get("workload_type") or "").lower() for item in items}
        assert "domain" in workload_types, (
            f"Expected 'domain' in workload_types, got {workload_types}"
        )

    def test_domain_workload_in_unfiltered_list(
        self,
        ros_api_url: str,
        domain_auth: dict,
        http_session: requests.Session,
    ):
        """Domain workloads appear alongside standard types (deployment,
        daemonset, etc.) in an unfiltered recommendation list.

        Skips if domain data has not been seeded.
        """
        body = _fetch_recommendations(
            ros_api_url, domain_auth, http_session,
            params={"limit": 200},
        )
        items = body.get("data") or []
        if not items:
            pytest.skip("No container recommendations in cluster")

        workload_types = {
            (item.get("workload_type") or "").lower()
            for item in items
            if item.get("workload_type")
        }

        if "domain" not in workload_types:
            pytest.skip(
                "No domain workload in unfiltered list — "
                "seed_domain.yml data may not have been ingested"
            )

        standard_types = {"deployment", "daemonset", "statefulset", "replicaset"}
        has_standard = bool(workload_types & standard_types)
        assert has_standard or len(workload_types) >= 2, (
            f"Expected domain alongside other workload types, got only {workload_types}"
        )

    def test_domain_workload_exclude_filter(
        self,
        ros_api_url: str,
        domain_auth: dict,
        http_session: requests.Session,
    ):
        """exclude[workload_type]=domain removes domain rows from results.

        First confirms domain data exists, then verifies the exclude filter
        omits it. Skips if domain data has not been seeded.
        """
        include_body = _fetch_recommendations(
            ros_api_url, domain_auth, http_session,
            params={"filter[workload_type]": "domain", "limit": 5},
        )
        domain_count = include_body.get("meta", {}).get("count", 0)
        if domain_count == 0:
            pytest.skip(
                "No domain workload recommendations to exclude — "
                "seed_domain.yml data may not have been ingested"
            )

        exclude_body = _fetch_recommendations(
            ros_api_url, domain_auth, http_session,
            params={"exclude[workload_type]": "domain", "limit": 200},
        )
        excluded_items = exclude_body.get("data") or []
        for item in excluded_items:
            wt = (item.get("workload_type") or "").lower()
            assert wt != "domain", (
                f"exclude[workload_type]=domain should omit domain rows, "
                f"but got workload_type={item.get('workload_type')!r}"
            )

        unfiltered_body = _fetch_recommendations(
            ros_api_url, domain_auth, http_session,
            params={"limit": 1},
        )
        total_count = unfiltered_body.get("meta", {}).get("count", 0)
        excluded_count = exclude_body.get("meta", {}).get("count", 0)
        if total_count > 0 and domain_count > 0:
            assert excluded_count < total_count, (
                f"Excluding domain should reduce count: "
                f"total={total_count}, after_exclude={excluded_count}"
            )

    def test_domain_workload_exclude_filter_accepted(
        self,
        ros_api_url: str,
        domain_auth: dict,
        http_session: requests.Session,
    ):
        """exclude[workload_type]=domain returns 200 (not 400), proving the
        API accepts exclude filters for arbitrary workload_type strings.

        This test does not require domain data to be present — it validates
        the API contract only.
        """
        body = _fetch_recommendations(
            ros_api_url, domain_auth, http_session,
            params={"exclude[workload_type]": "domain", "limit": 5},
        )
        items = body.get("data") or []
        for item in items:
            wt = (item.get("workload_type") or "").lower()
            assert wt != "domain", (
                f"exclude filter returned domain row: {item.get('workload_type')!r}"
            )
