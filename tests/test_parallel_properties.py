"""Property-based tests for parallel merge invariants.

Uses hypothesis to verify that _merge_parallel satisfies its contracts
across random combinations of node count, success/failure, and field values.

Verifies: P2, P3, P5, P8, P10
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from pymh.observe import _merge_parallel, _validate_observation

# --- Strategies ---

completeness_st = st.sampled_from(["full", "partial", "none"])
confidence_st = st.sampled_from(["low", "medium", "high"])
surprise_st = st.floats(min_value=0.0, max_value=1.0)
quality_st = st.integers(min_value=0, max_value=100)


def observation_st(node_id: str) -> st.SearchStrategy:
    """Strategy for a single validated observation."""
    return st.fixed_dictionaries({
        "success": st.booleans(),
        "signal": st.text(min_size=1, max_size=20),
        "surprise": surprise_st,
        "conditions": st.fixed_dictionaries({
            "quality_score": quality_st,
            "completeness": completeness_st,
            "blocker": st.one_of(st.none(), st.text(min_size=1, max_size=10)),
            "confidence": confidence_st,
            "needs_replan": st.booleans(),
            "escalate": st.booleans(),
        }),
        "evidence": st.fixed_dictionaries({}, optional={
            "tests_passing": st.booleans(),
            "build_success": st.booleans(),
            "artifact_exists": st.booleans(),
        }),
        "tags": st.dictionaries(
            st.text(min_size=1, max_size=5),
            st.text(min_size=1, max_size=5),
            max_size=3,
        ),
        "profile_updates": st.dictionaries(
            st.text(min_size=1, max_size=5),
            st.text(min_size=1, max_size=10),
            max_size=3,
        ),
        "narrative": st.text(max_size=50),
        "files_changed": st.lists(st.text(min_size=1, max_size=10), max_size=3),
    })


@st.composite
def parallel_observations_st(draw: st.DrawFn) -> list[tuple[str, dict]]:
    """Strategy for a list of (node_id, observation) pairs."""
    n = draw(st.integers(min_value=1, max_value=5))
    node_ids = [f"t{i}" for i in range(n)]
    observations = []
    for nid in node_ids:
        obs = draw(observation_st(nid))
        # Run through validation to simulate real pipeline (Verifies: P10)
        obs, _ = _validate_observation(obs)
        observations.append((nid, obs))
    return observations


# --- Invariant Tests ---


class TestMergeSuccessInvariant:
    """Verifies: P2 — merged success == all(individual successes)."""

    @given(observations=parallel_observations_st())
    @settings(max_examples=200)
    def test_success_is_all(self, observations: list[tuple[str, dict]]) -> None:
        merged = _merge_parallel(observations)
        expected = all(obs.get("success", False) for _, obs in observations)
        assert merged["success"] is expected


class TestMergeCompletenessInvariant:
    """Verifies: P2 — completeness is ALL-or-nothing."""

    @given(observations=parallel_observations_st())
    @settings(max_examples=200)
    def test_completeness_all_or_partial(self, observations: list[tuple[str, dict]]) -> None:
        merged = _merge_parallel(observations)
        all_full = all(
            obs.get("conditions", {}).get("completeness") == "full"
            for _, obs in observations
        )
        if all_full:
            assert merged["conditions"]["completeness"] == "full"
        else:
            assert merged["conditions"]["completeness"] == "partial"


class TestMergeQualityScoreInvariant:
    """Verifies: P2 — quality_score = MAX of individuals."""

    @given(observations=parallel_observations_st())
    @settings(max_examples=200)
    def test_quality_is_max(self, observations: list[tuple[str, dict]]) -> None:
        merged = _merge_parallel(observations)
        expected = max(
            obs.get("conditions", {}).get("quality_score", 50)
            for _, obs in observations
        )
        assert merged["conditions"]["quality_score"] == expected


class TestMergeSurpriseInvariant:
    """Verifies: P2 — surprise = MAX of individuals."""

    @given(observations=parallel_observations_st())
    @settings(max_examples=200)
    def test_surprise_is_max(self, observations: list[tuple[str, dict]]) -> None:
        merged = _merge_parallel(observations)
        expected = max(obs.get("surprise", 0.5) for _, obs in observations)
        # Float comparison with tolerance
        assert abs(merged["surprise"] - expected) < 1e-9


class TestMergeConfidenceInvariant:
    """Verifies: P2 — confidence = worst case (minimum)."""

    @given(observations=parallel_observations_st())
    @settings(max_examples=200)
    def test_confidence_is_min(self, observations: list[tuple[str, dict]]) -> None:
        merged = _merge_parallel(observations)
        conf_order = {"low": 0, "medium": 1, "high": 2}
        expected = min(
            (obs.get("conditions", {}).get("confidence", "low") for _, obs in observations),
            key=lambda c: conf_order.get(c, 0),
        )
        assert merged["conditions"]["confidence"] == expected


class TestMergeEscalateInvariant:
    """Verifies: P2 — escalate/needs_replan = ANY."""

    @given(observations=parallel_observations_st())
    @settings(max_examples=200)
    def test_escalate_is_any(self, observations: list[tuple[str, dict]]) -> None:
        merged = _merge_parallel(observations)
        expected = any(
            obs.get("conditions", {}).get("escalate", False) for _, obs in observations
        )
        assert merged["conditions"]["escalate"] is expected

    @given(observations=parallel_observations_st())
    @settings(max_examples=200)
    def test_needs_replan_is_any(self, observations: list[tuple[str, dict]]) -> None:
        merged = _merge_parallel(observations)
        expected = any(
            obs.get("conditions", {}).get("needs_replan", False) for _, obs in observations
        )
        assert merged["conditions"]["needs_replan"] is expected


class TestMergeEvidenceInvariant:
    """Verifies: P3 — evidence keyed by node-id, no data loss."""

    @given(observations=parallel_observations_st())
    @settings(max_examples=200)
    def test_evidence_keyed_by_node(self, observations: list[tuple[str, dict]]) -> None:
        merged = _merge_parallel(observations)
        for nid, obs in observations:
            if obs.get("evidence"):
                assert nid in merged["evidence"]
                assert merged["evidence"][nid] == obs["evidence"]


class TestMergeOutputShape:
    """Merged observation must have all required fields."""

    @given(observations=parallel_observations_st())
    @settings(max_examples=100)
    def test_has_required_fields(self, observations: list[tuple[str, dict]]) -> None:
        merged = _merge_parallel(observations)
        assert "success" in merged
        assert "signal" in merged
        assert "conditions" in merged
        assert "evidence" in merged
        assert "surprise" in merged
        assert isinstance(merged["success"], bool)
        assert isinstance(merged["signal"], str)
        assert isinstance(merged["conditions"], dict)
        assert isinstance(merged["evidence"], dict)
        assert isinstance(merged["surprise"], float)

    @given(observations=parallel_observations_st())
    @settings(max_examples=100)
    def test_conditions_has_core_fields(self, observations: list[tuple[str, dict]]) -> None:
        merged = _merge_parallel(observations)
        conds = merged["conditions"]
        assert "quality_score" in conds
        assert "completeness" in conds
        assert "confidence" in conds
        assert "needs_replan" in conds
        assert "escalate" in conds


class TestMergeBlockerInvariant:
    """Blockers are joined from all members."""

    @given(observations=parallel_observations_st())
    @settings(max_examples=100)
    def test_blocker_preserves_all(self, observations: list[tuple[str, dict]]) -> None:
        merged = _merge_parallel(observations)
        individual_blockers = [
            obs.get("conditions", {}).get("blocker")
            for _, obs in observations
            if obs.get("conditions", {}).get("blocker") is not None
        ]
        if not individual_blockers:
            assert merged["conditions"]["blocker"] is None
        else:
            for b in individual_blockers:
                assert str(b) in merged["conditions"]["blocker"]


class TestPreValidationEffect:
    """Verifies: P10 — pre-validation normalizes before merge."""

    @given(
        success=st.booleans(),
        quality=st.sampled_from(["85", "70", "not_a_number"]),
        replan=st.sampled_from(["true", "false", "True", "FALSE"]),
    )
    @settings(max_examples=50)
    def test_string_fields_coerced_before_merge(
        self, success: bool, quality: str, replan: str
    ) -> None:
        raw_obs = {
            "success": success,
            "signal": "test",
            "surprise": 0.3,
            "conditions": {
                "quality_score": quality,
                "needs_replan": replan,
                "completeness": "partial",
            },
        }
        validated, _ = _validate_observation(raw_obs)
        # After validation, types must be correct
        assert isinstance(validated["conditions"]["quality_score"], int)
        assert isinstance(validated["conditions"]["needs_replan"], bool)

        # Merge with validated input should not raise
        merged = _merge_parallel([("t1", validated)])
        assert isinstance(merged["conditions"]["quality_score"], int)
