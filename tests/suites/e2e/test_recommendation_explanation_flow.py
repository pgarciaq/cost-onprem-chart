"""
E2E: verify recommendation explanation factors via include=explanation.

After session data seeding generates recommendations, this test:
  1. Lists container recommendations and picks an existing UUID.
  2. GET detail with ?include=explanation.
  3. Asserts at least one explanation field is populated (not all null).

Skips when the cluster has no recommendations, the detail endpoint returns
404, or the deployed backend does not recognize include=explanation (older
images without Phase 3 explanation opt-in).
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from conftest import get_fresh_auth_header
from suites.ros.test_recommendations import get_recommendations_endpoint


def _container_detail_url(ros_api_url: str, recommendation_id: str) -> str:
    base = get_recommendations_endpoint(ros_api_url)
    return f"{base}/{recommendation_id}"


def _non_null_explanation_fields(detail: dict[str, Any]) -> dict[str, Any]:
    """Collect non-null explanation fields from container detail engines."""
    found: dict[str, Any] = {}
    terms = ((detail.get("recommendations") or {}).get("recommendation_terms") or {})
    if not isinstance(terms, dict):
        return found

    for term_name, term in terms.items():
        if not isinstance(term, dict):
            continue
        engines = term.get("recommendation_engines") or {}
        if not isinstance(engines, dict):
            continue
        for profile in ("cost", "performance"):
            eng = engines.get(profile) or {}
            if not isinstance(eng, dict):
                continue
            expl = eng.get("explanation")
            if not isinstance(expl, dict):
                continue
            for key, value in expl.items():
                if value is not None:
                    found[f"{term_name}.{profile}.{key}"] = value

    gpu_block = detail.get("gpu") or {}
    if isinstance(gpu_block, dict):
        for term_name, gpu_rec in gpu_block.items():
            if not isinstance(gpu_rec, dict):
                continue
            expl = gpu_rec.get("explanation")
            if not isinstance(expl, dict):
                continue
            for key, value in expl.items():
                if value is not None:
                    found[f"gpu.{term_name}.{key}"] = value

    return found


def _detail_has_explanation_key(detail: dict[str, Any]) -> bool:
    """Return True if any engine or GPU block includes an explanation object."""
    terms = ((detail.get("recommendations") or {}).get("recommendation_terms") or {})
    if isinstance(terms, dict):
        for term in terms.values():
            if not isinstance(term, dict):
                continue
            engines = term.get("recommendation_engines") or {}
            if not isinstance(engines, dict):
                continue
            for profile in ("cost", "performance"):
                eng = engines.get(profile) or {}
                if isinstance(eng, dict) and "explanation" in eng:
                    return True

    gpu_block = detail.get("gpu") or {}
    if isinstance(gpu_block, dict):
        for gpu_rec in gpu_block.values():
            if isinstance(gpu_rec, dict) and "explanation" in gpu_rec:
                return True
    return False


@pytest.mark.e2e
@pytest.mark.integration
@pytest.mark.ros
class TestRecommendationExplanationFlow:
    """Verify explanation factors are exposed when include=explanation is requested."""

    def test_container_detail_include_explanation_populated(
        self,
        ros_api_url: str,
        keycloak_config,
        cluster_config,
        http_session: requests.Session,
    ):
        auth_header = get_fresh_auth_header(keycloak_config, http_session)
        if not auth_header:
            pytest.skip("Could not obtain fresh JWT token")

        list_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=auth_header,
            params={"limit": 1},
            timeout=60,
        )
        assert list_resp.status_code == 200, list_resp.text
        items = list_resp.json().get("data") or []
        if not items:
            pytest.skip("No container recommendations in cluster")

        rec_id = items[0].get("id")
        assert rec_id, "list item must include id"

        detail_resp = http_session.get(
            _container_detail_url(ros_api_url, rec_id),
            headers=auth_header,
            params={"include": "explanation"},
            timeout=60,
        )
        if detail_resp.status_code == 404:
            pytest.skip("Container detail endpoint returned 404 (recommendation not found)")
        assert detail_resp.status_code == 200, detail_resp.text

        detail = detail_resp.json()
        if not _detail_has_explanation_key(detail):
            pytest.skip(
                "Backend does not expose explanation with include=explanation "
                "(older ROS API without explanation opt-in)"
            )

        populated = _non_null_explanation_fields(detail)
        assert populated, (
            "Expected at least one non-null explanation field when include=explanation; "
            f"detail keys checked in recommendation_terms/gpu blocks"
        )

        # data_days is the canonical persisted explanation factor for containers.
        assert any(
            key.endswith(".data_days") for key in populated
        ), f"Expected data_days among populated explanation fields, got: {sorted(populated)}"
