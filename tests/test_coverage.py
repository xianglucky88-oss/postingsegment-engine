import math
import random
import pytest
from postingsegment import coverage, spatial


def fp(fid, rect, identity=None):
    return {"id": fid, "identity": identity or fid, "rectangle": rect}


def test_overlapping_footprints_union_not_naive_count():
    # Two half-area footprints overlap on a quarter of the 4x4 zone: naive
    # summation gives 8, the true union is 6.
    result = coverage.analyze_coverage(
        [fp("a", [0, 0, 2, 2]), fp("b", [0, 0, 2, 2])],
        [{"id": "z", "rectangle": [0, 0, 4, 4]}],
    )
    z = result["zones"][0]
    assert z["status"] == "complete"
    assert z["naive_intersections"] == 2
    assert math.isclose(z["covered_area"], 4.0)
    assert math.isclose(z["coverage_rate"], 0.25)
    assert math.isclose(z["naive_intersection_area_estimate"], 8.0)
    assert math.isclose(z["redundant_cross_identity_area"], 4.0)
    assert math.isclose(z["redundant_same_identity_area"], 0.0)
    assert result["conservation"]["status"] == "complete"


def test_duplicate_identity_records_never_expand_union():
    result = coverage.analyze_coverage(
        [fp("r1", [0, 0, 2, 2], identity="obs"), fp("r2", [1, 1, 3, 3], identity="obs")],
        [{"id": "z", "rectangle": [0, 0, 5, 5]}],
    )
    z = result["zones"][0]
    # Union area of the two rectangles is 4 + 4 - 1 = 7, counted once.
    assert math.isclose(z["covered_area"], 7.0)
    assert z["effective_identities"] == ["obs"]
    assert math.isclose(z["redundant_same_identity_area"], 1.0)
    assert math.isclose(z["redundant_cross_identity_area"], 0.0)
    assert result["duplicate_identities"] == {"obs": ["r1", "r2"]}
    assert math.isclose(result["conservation"]["identity_union_area"], 7.0)


def test_cross_zone_footprint_clipped_per_zone_and_conserved():
    footprints = [fp("wide", [2, 2, 8, 8])]
    zones = [{"id": "lo", "rectangle": [0, 0, 5, 10]},
             {"id": "hi", "rectangle": [5, 0, 10, 10]}]
    result = coverage.analyze_coverage(footprints, zones)
    lo, hi = result["zones"]
    assert math.isclose(lo["covered_area"], 3 * 6)
    assert math.isclose(hi["covered_area"], 3 * 6)
    assert result["conservation"]["status"] == "complete"
    assert math.isclose(result["overall"]["covered_area"], 36.0)


def test_boundary_only_contact_contributes_nothing():
    result = coverage.analyze_coverage(
        [fp("touch", [5, 0, 5, 4]), fp("edge", [5, 2, 7, 4])],
        [{"id": "z", "rectangle": [0, 0, 5, 10]}],
    )
    z = result["zones"][0]
    # The zero-area rectangle intersects per the closed-box index but has no
    # interior; only the second rectangle's inside piece counts.
    assert z["naive_intersections"] == 2
    assert math.isclose(z["covered_area"], 0.0)
    assert result["footprints_outside_partition"] == ["edge", "touch"]


def test_footprint_entirely_outside_is_listed_not_dropped():
    result = coverage.analyze_coverage(
        [fp("out", [9, 9, 10, 10]), fp("in", [0, 0, 1, 1])],
        [{"id": "z", "rectangle": [0, 0, 5, 5]}],
    )
    assert result["footprints_outside_partition"] == ["out"]
    assert math.isclose(result["zones"][0]["covered_area"], 1.0)


def test_weighted_coverage_excludes_unknown_weights():
    units = [
        {"id": "u1", "rectangle": [0, 0, 1, 1], "weight": 3.0, "stratum": "s"},
        {"id": "u2", "rectangle": [1, 1, 2, 2], "weight": 1.0, "stratum": "s"},
        {"id": "u3", "rectangle": [2, 2, 3, 3], "weight": None, "stratum": "s"},
    ]
    result = coverage.analyze_coverage(
        [fp("full", [0, 0, 1, 1])],
        [{"id": "z", "rectangle": [0, 0, 4, 4]}],
        units,
    )
    z = result["zones"][0]
    assert z["unknown_weight_units"] == ["u3"]
    # Effective denominator contains known weights only.
    assert math.isclose(z["effective_denominator"], 4.0)
    assert math.isclose(z["weighted_coverage_rate"], 0.75)
    assert math.isclose(result["overall"]["effective_denominator"], 4.0)
    row = {r["unit"]: r for r in z["units"]}
    assert row["u1"]["covered"] is True
    assert row["u2"]["covered_fraction"] == 0.0 and row["u2"]["covered"] is False
    assert row["u3"]["covered_fraction"] == 0.0 and row["u3"]["weight"] is None


def test_partial_coverage_threshold_metric():
    units = [{"id": "u", "rectangle": [0, 0, 2, 2], "weight": 1.0}]
    result = coverage.analyze_coverage(
        [fp("half", [0, 0, 2, 1])],
        [{"id": "z", "rectangle": [0, 0, 4, 4]}],
        units,
        threshold=0.5,
    )
    row = result["zones"][0]["units"][0]
    assert math.isclose(row["covered_fraction"], 0.5)
    assert row["covered"] is True  # fraction >= threshold
    strict = coverage.analyze_coverage(
        [fp("half", [0, 0, 2, 1])],
        [{"id": "z", "rectangle": [0, 0, 4, 4]}],
        units,
        threshold=0.51,
    )
    assert strict["zones"][0]["units"][0]["covered"] is False


def test_zero_area_reference_unit_flagged():
    result = coverage.analyze_coverage(
        [fp("f", [0, 0, 1, 1])],
        [{"id": "z", "rectangle": [0, 0, 4, 4]}],
        [{"id": "line", "rectangle": [1, 1, 1, 3], "weight": 2.0}],
    )
    row = result["zones"][0]["units"][0]
    assert row["zero_area"] is True
    assert row["covered_fraction"] is None and row["covered"] is False
    assert result["zones"][0]["zero_area_units"] == ["line"]
    # Its weight stays in the denominator but cannot accrue covered weight.
    assert math.isclose(result["zones"][0]["effective_denominator"], 2.0)
    assert result["zones"][0]["weighted_coverage_rate"] == 0.0


def test_gap_attribution_observation_vs_reference():
    # Reference fills only the left half; right half has no reference data.
    result = coverage.analyze_coverage(
        [fp("f", [0, 0, 1, 4])],
        [{"id": "z", "rectangle": [0, 0, 4, 4]}],
        [{"id": "u", "rectangle": [0, 0, 2, 4], "weight": 1.0}],
    )
    z = result["zones"][0]
    assert math.isclose(z["reference_area"], 8.0)
    assert math.isclose(z["reference_covered_area"], 4.0)
    # Observation gap: referenced but unobserved (x in 1..2 strip).
    assert math.isclose(z["observation_gap_area"], 4.0)
    # Reference gap: no reference material at all (x in 2..4 strip).
    assert math.isclose(z["reference_gap_area"], 8.0)


def test_stratified_differences():
    units = [
        {"id": "a", "rectangle": [0, 0, 1, 1], "weight": 1.0, "stratum": "urban"},
        {"id": "b", "rectangle": [1, 1, 2, 2], "weight": 1.0, "stratum": "rural"},
    ]
    result = coverage.analyze_coverage(
        [fp("f", [0, 0, 1, 1])],
        [{"id": "z", "rectangle": [0, 0, 4, 4]}],
        units,
    )
    rows = {r["stratum"]: r for r in result["strata"]}
    assert rows["urban"]["weighted_coverage_rate"] == 1.0
    assert rows["rural"]["weighted_coverage_rate"] == 0.0
    assert rows["urban"]["covered_units"] == 1 and rows["rural"]["covered_units"] == 0


def test_covered_unit_count_not_double_counted():
    # Two fully covered units must report two, proving per-unit counting once.
    units = [
        {"id": "a", "rectangle": [0, 0, 1, 1], "weight": 1.0, "stratum": "s"},
        {"id": "b", "rectangle": [1, 1, 2, 2], "weight": 1.0, "stratum": "s"},
        {"id": "c", "rectangle": [2, 2, 3, 3], "weight": 1.0, "stratum": "s"},
    ]
    result = coverage.analyze_coverage(
        [fp("f", [0, 0, 2, 2])],
        [{"id": "z", "rectangle": [0, 0, 4, 4]}],
        units,
    )
    z = result["zones"][0]
    assert z["covered_reference_units"] == 2
    row = {r["unit"]: r for r in z["units"]}
    assert [row[u]["covered"] for u in ("a", "b", "c")] == [True, True, False]
    assert result["strata"][0]["covered_units"] == 2
    assert result["strata"][0]["units"] == 3


def test_zero_threshold_covers_any_positive_fraction():
    result = coverage.analyze_coverage(
        [fp("f", [0, 0, 1, 1])],
        [{"id": "z", "rectangle": [0, 0, 4, 4]}],
        [{"id": "u", "rectangle": [0, 0, 2, 2], "weight": 1.0}],
        threshold=0.0,
    )
    assert result["zones"][0]["units"][0]["covered"] is True
    with pytest.raises(ValueError):
        coverage.analyze_coverage(
            [fp("f", [0, 0, 1, 1])],
            [{"id": "z", "rectangle": [0, 0, 4, 4]}],
            threshold=1.1,
        )


def test_budget_exhaustion_marks_zones_incomplete_not_zero():
    # Interlocking thin rectangles create many compressed cells; a tiny budget
    # cannot finish the exact grid.
    footprints = [fp("f%d" % i, [i * 0.5, 0, i * 0.5 + 0.6, 10]) for i in range(20)]
    zones = [{"id": "big", "rectangle": [0, 0, 12, 10]}]
    result = coverage.analyze_coverage(footprints, zones, budget=100)
    assert result["status"] == "incomplete"
    assert result["incomplete_zones"] == ["big"]
    assert result["zones"][0]["status"] == "incomplete"
    assert result["overall"]["coverage_rate"] is None
    assert result["conservation"]["status"] == "skipped"
    # Structural facts are still reported.
    assert result["zones"][0]["naive_intersections"] == 20
    assert result["overall"]["zone_area"] == 120.0


def test_conservation_holds_under_random_skew():
    rng = random.Random(17)
    footprints, zones, units = [], [], []
    for k in range(2):
        x0, y0 = k * 10, 0
        zones.append({"id": "z%d" % k, "rectangle": [x0, y0, x0 + 10, 10]})
        units.append({"id": "u%d" % k, "rectangle": [x0, y0, x0 + 5, 10],
                      "weight": float(k + 1), "stratum": "s%d" % k})
    for i in range(40):
        x = rng.uniform(0, 18)
        y = rng.uniform(0, 8)
        ident = "obs-%d" % (i % 12)  # force duplicate identities
        footprints.append(fp("r%d" % i, [x, y, min(x + rng.uniform(0.5, 3), 20),
                                         min(y + rng.uniform(0.5, 3), 10)], ident))
    result = coverage.analyze_coverage(footprints, zones, units)
    assert result["status"] == "complete"
    cons = result["conservation"]
    assert cons["status"] == "complete"
    assert all(cons["checks"]["per_zone"].values())
    assert math.isclose(cons["zoned_covered_area"], cons["per_zone_sum"])
    # Independent union recomputation matches zone-split merge.
    merged = sum(z["covered_area"] for z in result["zones"])
    assert math.isclose(cons["zoned_covered_area"], merged)


def test_overlapping_zones_and_bad_geometry_rejected():
    with pytest.raises(ValueError):
        coverage.analyze_coverage(
            [fp("f", [0, 0, 1, 1])],
            [{"id": "a", "rectangle": [0, 0, 2, 2]}, {"id": "b", "rectangle": [1, 1, 3, 3]}],
        )
    with pytest.raises(ValueError):
        coverage.analyze_coverage(
            [fp("f", [0, 0, 1, 1])],
            [{"id": "z", "rectangle": [0, 0, 0, 5]}],
        )
    with pytest.raises(ValueError):
        coverage.analyze_coverage(
            [fp("f", [2, 0, 1, 1])],
            [{"id": "z", "rectangle": [0, 0, 5, 5]}],
        )
    with pytest.raises(ValueError):
        coverage.analyze_coverage(
            [fp("f", [0, 0, 1, 1])],
            [{"id": "z", "rectangle": [0, 0, 5, 5]}],
            [{"id": "u", "rectangle": [6, 6, 7, 7], "weight": 1.0}],
        )
    with pytest.raises(ValueError):
        coverage.analyze_coverage(
            [fp("f", [0, 0, 1, 1])],
            [{"id": "z", "rectangle": [0, 0, 5, 5]}],
            [{"id": "u", "rectangle": [0, 0, 1, 1], "weight": -1.0}],
        )


def test_index_candidates_match_bruteforce():
    # The read-only spatial index must still be the candidate source: verify it
    # returns exactly the closed-box intersections the aggregation then clips.
    rng = random.Random(11)
    items = {str(i): [rng.randrange(10), rng.randrange(10),
                      rng.randrange(10, 15), rng.randrange(10, 15)] for i in range(30)}
    idx = spatial.SpatialIndex(items)
    zone = [2, 2, 8, 8]
    expected = sorted(k for k, b in items.items()
                      if b[0] <= zone[2] and zone[0] <= b[2]
                      and b[1] <= zone[3] and zone[1] <= b[3])
    assert idx.query(zone) == expected


def test_no_reference_units_rates_and_gap_labels():
    result = coverage.analyze_coverage(
        [fp("f", [0, 0, 2, 2])],
        [{"id": "z", "rectangle": [0, 0, 4, 4]}],
    )
    z = result["zones"][0]
    assert z["weighted_coverage_rate"] is None
    assert z["effective_denominator"] == 0.0
    # Without reference material every uncovered cell is a reference gap.
    assert math.isclose(z["reference_gap_area"], 12.0)
    assert math.isclose(z["observation_gap_area"], 0.0)
    assert result["overall"]["weighted_coverage_rate"] is None
