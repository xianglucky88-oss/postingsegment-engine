"""Spatial coverage and representativeness analysis.

Inputs are axis-aligned rectangles in a single planar coordinate system:
observation footprints, non-overlapping zones and reference units carrying
explicit weights. Naively counting index intersections overstates coverage when
footprints cluster and overlap, so every reported area is derived from an exact
union on an exhaustive coordinate-compressed grid whose cells each have constant
membership. The read-only :class:`~postingsegment.spatial.SpatialIndex` is used
only to locate candidate footprints; summation itself never double counts.

Distinct situations deliberately use distinct rules:

* a footprint crossing a zone is clipped per zone and reported once per zone it
  actually covers;
* rectangles that only touch a boundary contribute zero area, never a hit;
* zero-area reference units are flagged, not treated as covered or uncovered;
* reference units without a known, positive weight are excluded from weighted
  denominators and listed explicitly, so unknown weights never inflate coverage;
* repeated records carrying a source identity are merged by identity, so
  duplicates cannot enlarge the real covered area.

When the exact grid would exceed the work budget the affected zones (and the
independent conservation grid) are reported as unfinished instead of being
silently counted as zero coverage.
"""
import math

from .spatial import SpatialIndex, box, intersects

_EPS = 1e-12
_DEFAULT_THRESHOLD = 1.0
_DEFAULT_BUDGET = 1_000_000
_GRID_WORK_FACTOR = 8


class _BudgetExceeded(Exception):
    """Internal signal that an exact grid could not fit the work budget."""


def _rect(value, label):
    try:
        return box(value)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("%s: %s" % (label, exc))


def _finite_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a number" % label)
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("%s must be finite" % label)
    return value


def _clip(rect, zone):
    x0 = max(rect[0], zone[0])
    y0 = max(rect[1], zone[1])
    x1 = min(rect[2], zone[2])
    y1 = min(rect[3], zone[3])
    if x0 >= x1 or y0 >= y1:
        return None  # no interior: disjoint or boundary-only contact
    return (x0, y0, x1, y1)


def _grid_build(xs, ys, rects, budget_left):
    """Build an exhaustive compressed grid.

    ``xs``/``ys`` are sorted unique coordinates; every rectangle edge lies on a
    grid line, hence each resulting cell has constant membership. ``rects`` is
    a list of ``(x0, y0, x1, y1, tag)`` whose tag is ignored. Returns
    ``(nx, ny, counts, work)`` with one nonnegative stabbing count per cell.
    """
    nx, ny = len(xs) - 1, len(ys) - 1
    cells = nx * ny
    incidences = len(rects)
    work = cells + incidences
    if cells <= 0 or work > budget_left:
        raise _BudgetExceeded(work)
    # Negative corners land on the nx/ny boundary, so keep one spare row/column.
    diff = [0] * (nx + 1) * (ny + 1)

    def index(ix, iy):
        return iy * (nx + 1) + ix

    for x0, y0, x1, y1, _ in rects:
        i0 = xs.index(x0)
        i1 = xs.index(x1)
        j0 = ys.index(y0)
        j1 = ys.index(y1)
        diff[index(i0, j0)] += 1
        diff[index(i1, j1)] += 1
        diff[index(i1, j0)] -= 1
        diff[index(i0, j1)] -= 1
    # Row-wise then column-wise prefix sums turn the difference array into the
    # per-cell stabbing count over the nx*ny interior.
    for j in range(ny):
        base = j * (nx + 1)
        run = 0
        for i in range(nx):
            run += diff[base + i]
            diff[base + i] = run
    counts = [0] * cells
    for i in range(nx):
        run = 0
        for j in range(ny):
            run += diff[j * (nx + 1) + i]
            counts[j * nx + i] = run
    return nx, ny, counts, work


def _rect_areas(xs, ys, rects):
    """Exact covered and redundant areas on a compressed grid.

    Cells are visited once per stabbing rectangle, so the work stays
    proportional to rectangle-cell incidences. Same-identity redundancy is the
    area covered more than once by records carrying the same source identity;
    cross-identity redundancy is excess coverage from distinct identities.
    """
    nx, ny = len(xs) - 1, len(ys) - 1

    # Precompute cell membership lists from the tagged rectangles.
    members = [None] * (nx * ny)
    incidence = 0
    for x0, y0, x1, y1, tag in rects:
        i0, i1 = xs.index(x0), xs.index(x1)
        j0, j1 = ys.index(y0), ys.index(y1)
        for j in range(j0, j1):
            base = j * nx
            for i in range(i0, i1):
                k = base + i
                if members[k] is None:
                    members[k] = []
                members[k].append(tag)
                incidence += 1
    widths = [xs[i + 1] - xs[i] for i in range(nx)]
    covered = 0.0
    redundant_same = 0.0
    redundant_cross = 0.0
    work = nx * ny + incidence
    for j in range(ny):
        h = ys[j + 1] - ys[j]
        base = j * nx
        for i in range(nx):
            tags = members[base + i]
            if not tags:
                continue
            area = widths[i] * h
            covered += area
            unique = set(tags)
            same = len(tags) - len(unique)  # duplicate records of one identity
            if same:
                redundant_same += area * same
            if len(unique) > 1:
                redundant_cross += area * (len(unique) - 1)
    return {
        "covered_area": covered,
        "redundant_same_identity_area": redundant_same,
        "redundant_cross_identity_area": redundant_cross,
        "work": work,
    }


def _spanned_cells(xs, ys, rects):
    total = 0
    for x0, y0, x1, y1, _ in rects:
        total += (xs.index(x1) - xs.index(x0)) * (ys.index(y1) - ys.index(y0))
    return total


def _fraction_inside(xs, ys, width, counts, rect):
    """Area-weighted fraction of positive-area ``rect`` covered on the grid."""
    x0, y0, x1, y1 = rect
    i0, i1 = xs.index(x0), xs.index(x1)
    j0, j1 = ys.index(y0), ys.index(y1)
    total, inside = 0.0, 0.0
    for j in range(j0, j1):
        h = ys[j + 1] - ys[j]
        base = j * width
        for i in range(i0, i1):
            area = (xs[i + 1] - xs[i]) * h
            total += area
            if counts[base + i] > 0:
                inside += area
    return inside / total


def _close(a, b):
    scale = max(1.0, abs(a), abs(b))
    return abs(a - b) <= _EPS * scale


def analyze_coverage(footprints, zones, reference_units=None, *, threshold=_DEFAULT_THRESHOLD, budget=_DEFAULT_BUDGET):
    """Compute exact coverage and reference representativeness over a snapshot.

    ``footprints`` is a list of ``{"id", "rectangle", "identity"}`` records;
    records sharing an ``identity`` describe the same real-world observation and
    are merged. ``zones`` is a list of ``{"id", "rectangle"}`` partitions whose
    interiors must be disjoint. ``reference_units`` is an optional list of
    ``{"id", "rectangle", "weight", "stratum"}``; ``weight`` may be ``None`` for
    unknown weights and ``stratum`` defaults to ``"default"``. ``threshold`` is
    the covered-area fraction at/above which a reference unit counts as covered
    in the binary metric.

    The analysis is exact while the compressed grids fit ``budget`` work units;
    otherwise affected zones and the independent conservation grid are reported
    as unfinished, never as zero coverage.
    """
    if not isinstance(budget, int) or budget < 1:
        raise ValueError("budget must be a positive integer")
    threshold = _finite_number(threshold, "threshold")
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be within [0, 1]")
    if not isinstance(footprints, list) or not footprints:
        raise ValueError("footprints must be a non-empty list")
    if not isinstance(zones, list) or not zones:
        raise ValueError("zones must be a non-empty list")

    records = []
    identities = {}
    seen_ids = set()
    for pos, item in enumerate(footprints):
        if not isinstance(item, dict):
            raise ValueError("footprint %d must be an object" % pos)
        fid = str(item.get("id", pos))
        if fid in seen_ids:
            raise ValueError("duplicate footprint id %r" % fid)
        seen_ids.add(fid)
        rect = _rect(item["rectangle"], "footprint %r" % fid)
        identity = str(item.get("identity", fid))
        identities.setdefault(identity, []).append(fid)
        records.append({"id": fid, "rectangle": rect, "identity": identity})
    if len(records) > 10000:
        raise ValueError("geometry limit")

    zone_list = []
    zone_ids = set()
    for pos, item in enumerate(zones):
        if not isinstance(item, dict):
            raise ValueError("zone %d must be an object" % pos)
        zid = str(item.get("id", pos))
        if zid in zone_ids:
            raise ValueError("duplicate zone id %r" % zid)
        zone_ids.add(zid)
        rect = _rect(item["rectangle"], "zone %r" % zid)
        if rect[0] == rect[2] or rect[1] == rect[3]:
            raise ValueError("zone %r must have positive area" % zid)
        zone_list.append({"id": zid, "rectangle": rect})
    for a in range(len(zone_list)):
        for b in range(a + 1, len(zone_list)):
            ra, rb = zone_list[a]["rectangle"], zone_list[b]["rectangle"]
            if intersects(ra, rb) and not _boundary_only(ra, rb):
                raise ValueError("zones %r and %r have overlapping interiors" % (zone_list[a]["id"], zone_list[b]["id"]))

    units = []
    unit_ids = set()
    for pos, item in enumerate(reference_units or []):
        if not isinstance(item, dict):
            raise ValueError("reference unit %d must be an object" % pos)
        uid = str(item.get("id", pos))
        if uid in unit_ids:
            raise ValueError("duplicate reference unit id %r" % uid)
        unit_ids.add(uid)
        rect = _rect(item["rectangle"], "reference unit %r" % uid)
        weight = item.get("weight", None)
        if weight is not None:
            weight = _finite_number(weight, "weight of unit %r" % uid)
            if weight < 0.0:
                raise ValueError("weight of unit %r must be nonnegative" % uid)
        owner = _owning_zone(rect, zone_list)
        if owner is None:
            raise ValueError("reference unit %r is not contained in any zone" % uid)
        units.append({
            "id": uid, "rectangle": rect, "weight": weight,
            "stratum": str(item.get("stratum", "default")),
            "zone": owner,
            "zero_area": rect[0] == rect[2] or rect[1] == rect[3],
        })
    for a in range(len(units)):
        for b in range(a + 1, len(units)):
            ra, rb = units[a]["rectangle"], units[b]["rectangle"]
            if intersects(ra, rb) and not _boundary_only(ra, rb):
                raise ValueError("reference units %r and %r have overlapping interiors" % (units[a]["id"], units[b]["id"]))

    index = SpatialIndex({str(i): r["rectangle"] for i, r in enumerate(records)})
    remaining = budget
    zone_results = []
    incomplete_zones = []
    totals = {"zone_area": 0.0, "covered_area": 0.0, "effective_denominator": 0.0,
              "weighted_covered": 0.0, "naive_intersections": 0,
              "redundant_same_identity_area": 0.0, "redundant_cross_identity_area": 0.0,
              "reference_area": 0.0, "reference_covered_area": 0.0}
    strata = {}

    for zone in zone_list:
        zid, zrect = zone["id"], zone["rectangle"]
        zarea = (zrect[2] - zrect[0]) * (zrect[3] - zrect[1])
        totals["zone_area"] += zarea
        candidates = index.query(zrect)
        clipped = []
        naive = 0
        effective_ids = set()
        for key in candidates:
            rec = records[int(key)]
            naive += 1
            piece = _clip(rec["rectangle"], zrect)
            if piece is None:
                continue  # boundary-only contact contributes no area
            effective_ids.add(rec["identity"])
            clipped.append((piece[0], piece[1], piece[2], piece[3], rec["identity"]))
        totals["naive_intersections"] += naive

        zone_units = [u for u in units if u["zone"] == zid]
        xs = {zrect[0], zrect[2]}
        ys = {zrect[1], zrect[3]}
        for c in clipped:
            xs.update((c[0], c[2]))
            ys.update((c[1], c[3]))
        for u in zone_units:
            xs.update((u["rectangle"][0], u["rectangle"][2]))
            ys.update((u["rectangle"][1], u["rectangle"][3]))
        xs, ys = sorted(xs), sorted(ys)

        base_result = {
            "zone": zid, "zone_area": zarea, "naive_intersections": naive,
            "effective_identities": sorted(effective_ids),
            "reference_units": len(zone_units),
        }
        try:
            nx, ny = len(xs) - 1, len(ys) - 1
            incidence = _spanned_cells(xs, ys, clipped)
            unit_incidence = _spanned_cells(
                xs, ys, [(u["rectangle"][0], u["rectangle"][1], u["rectangle"][2],
                          u["rectangle"][3], 0) for u in zone_units if not u["zero_area"]]
            )
            # Main diff grid, membership grid, reference-mask grid plus the
            # cell scans for gaps and unit fractions.
            work = 4 * nx * ny + 2 * incidence + unit_incidence
            if work > remaining:
                raise _BudgetExceeded(work)
            nx, ny, counts, _ = _grid_build(xs, ys, clipped, remaining)
            areas = _rect_areas(xs, ys, clipped)
        except _BudgetExceeded:
            incomplete_zones.append(zid)
            base_result.update({"status": "incomplete", "reason": "budget exhausted"})
            zone_results.append(base_result)
            continue
        remaining -= work

        covered = areas["covered_area"]
        totals["covered_area"] += covered
        totals["redundant_same_identity_area"] += areas["redundant_same_identity_area"]
        totals["redundant_cross_identity_area"] += areas["redundant_cross_identity_area"]

        # Reference-mask grid marks cells lying inside any positive-area unit.
        ref_rects = [(u["rectangle"][0], u["rectangle"][1], u["rectangle"][2], u["rectangle"][3], 1)
                     for u in zone_units if not u["zero_area"]]
        _, _, ref_counts, _ = _grid_build(xs, ys, ref_rects, remaining)
        ref_area = sum(
            (xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j])
            for j in range(ny) for i in range(nx) if ref_counts[j * nx + i] > 0
        )
        ref_covered = sum(
            (xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j])
            for j in range(ny) for i in range(nx)
            if ref_counts[j * nx + i] > 0 and counts[j * nx + i] > 0
        )
        totals["reference_area"] += ref_area
        totals["reference_covered_area"] += ref_covered

        unit_rows = []
        effective_weight = 0.0
        weighted_covered = 0.0
        covered_units = 0
        unknown_weight_units = []
        zero_area_units = []
        for u in zone_units:
            if u["zero_area"]:
                fraction = None
                zero_area_units.append(u["id"])
            else:
                fraction = _fraction_inside(xs, ys, nx, counts, u["rectangle"])
            is_covered = fraction is not None and fraction + _EPS >= threshold
            row = {
                "unit": u["id"], "stratum": u["stratum"], "weight": u["weight"],
                "covered_fraction": fraction, "covered": bool(is_covered),
                "zero_area": u["zero_area"],
            }
            unit_rows.append(row)
            st = strata.setdefault(u["stratum"], {"units": 0, "covered_units": 0,
                                                  "effective_denominator": 0.0,
                                                  "weighted_covered": 0.0})
            st["units"] += 1
            if is_covered:
                covered_units += 1
                st["covered_units"] += 1
            if u["weight"] is None:
                unknown_weight_units.append(u["id"])
                continue
            if u["zero_area"]:
                # Weighted but area-less units keep their weight in the
                # denominator; they cannot be covered by an area footprint.
                effective_weight += u["weight"]
                st["effective_denominator"] += u["weight"]
                continue
            effective_weight += u["weight"]
            weighted_covered += u["weight"] * fraction
            st["effective_denominator"] += u["weight"]
            st["weighted_covered"] += u["weight"] * fraction
        totals["effective_denominator"] += effective_weight
        totals["weighted_covered"] += weighted_covered

        uncovered = zarea - covered
        observation_gap = sum(
            (xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j])
            for j in range(ny) for i in range(nx)
            if counts[j * nx + i] == 0 and ref_counts[j * nx + i] > 0
        )
        reference_gap = uncovered - observation_gap
        if abs(reference_gap) <= _EPS * max(1.0, zarea):
            reference_gap = 0.0
        # Conservation within a zone: the reference material partitions into
        # observed and unobserved cells, while the two gap types tile the rest.
        if reference_gap < 0 or not _close(ref_covered + observation_gap, ref_area) \
                or not _close(covered + observation_gap + reference_gap, zarea):
            raise ValueError("reference partition does not reconcile for zone %r" % zid)

        base_result.update({
            "status": "complete",
            "covered_area": covered,
            "coverage_rate": covered / zarea,
            "naive_intersection_area_estimate": sum(
                (c[2] - c[0]) * (c[3] - c[1]) for c in clipped
            ),
            "redundant_same_identity_area": areas["redundant_same_identity_area"],
            "redundant_cross_identity_area": areas["redundant_cross_identity_area"],
            "effective_denominator": effective_weight,
            "weighted_coverage_rate": (weighted_covered / effective_weight) if effective_weight > 0 else None,
            "covered_reference_units": covered_units,
            "unknown_weight_units": unknown_weight_units,
            "zero_area_units": zero_area_units,
            "reference_area": ref_area,
            "reference_covered_area": ref_covered,
            "observation_gap_area": observation_gap,
            "reference_gap_area": reference_gap,
            "units": unit_rows,
        })
        zone_results.append(base_result)

    # Footprints whose interiors miss every zone are reported, not dropped.
    outside = []
    for rec in records:
        owners = [z["id"] for z in zone_list
                  if intersects(rec["rectangle"], z["rectangle"])
                  and _clip(rec["rectangle"], z["rectangle"]) is not None]
        if not owners:
            outside.append(rec["id"])

    duplicate_identities = {ident: ids for ident, ids in identities.items() if len(ids) > 1}

    complete = not incomplete_zones
    overall = {
        "zone_area": totals["zone_area"],
        "covered_area": totals["covered_area"] if complete else None,
        "coverage_rate": (totals["covered_area"] / totals["zone_area"]) if complete else None,
        "naive_intersections": totals["naive_intersections"],
        "redundant_same_identity_area": totals["redundant_same_identity_area"] if complete else None,
        "redundant_cross_identity_area": totals["redundant_cross_identity_area"] if complete else None,
        "effective_denominator": totals["effective_denominator"] if complete else None,
        "weighted_coverage_rate": (totals["weighted_covered"] / totals["effective_denominator"])
        if complete and totals["effective_denominator"] > 0 else None,
        "reference_area": totals["reference_area"] if complete else None,
        "reference_covered_area": totals["reference_covered_area"] if complete else None,
    }
    stratum_rows = []
    for name in sorted(strata):
        st = strata[name]
        stratum_rows.append({
            "stratum": name,
            "units": st["units"],
            "covered_units": st["covered_units"],
            "effective_denominator": st["effective_denominator"],
            "weighted_coverage_rate": (st["weighted_covered"] / st["effective_denominator"])
            if complete and st["effective_denominator"] > 0 else None,
        })

    conservation = _conservation(records, zone_list, zone_results, budget) if complete else {
        "status": "skipped", "reason": "incomplete zone analysis",
    }

    return {
        "status": "complete" if complete else "incomplete",
        "threshold": threshold,
        "budget": budget,
        "overall": overall,
        "zones": zone_results,
        "strata": stratum_rows,
        "duplicate_identities": {k: sorted(v) for k, v in sorted(duplicate_identities.items())},
        "footprints_outside_partition": sorted(outside),
        "incomplete_zones": incomplete_zones,
        "conservation": conservation,
    }


def _boundary_only(a, b):
    inner_x = min(a[2], b[2]) - max(a[0], b[0])
    inner_y = min(a[3], b[3]) - max(a[1], b[1])
    return inner_x <= 0 or inner_y <= 0


def _owning_zone(rect, zone_list):
    for zone in zone_list:
        z = zone["rectangle"]
        if z[0] <= rect[0] and rect[2] <= z[2] and z[1] <= rect[1] and rect[3] <= z[3]:
            return zone["id"]
    return None


def _conservation(records, zone_list, zone_results, budget):
    """Independent whole-extent grid cross-check of per-zone split/merge."""
    all_rects = [r["rectangle"] for r in records]
    xs = sorted({c for r in all_rects + [z["rectangle"] for z in zone_list] for c in (r[0], r[2])})
    ys = sorted({c for r in all_rects + [z["rectangle"] for z in zone_list] for c in (r[1], r[3])})
    nx, ny = len(xs) - 1, len(ys) - 1
    footprint_rects = [(r["rectangle"][0], r["rectangle"][1], r["rectangle"][2], r["rectangle"][3],
                        r["identity"]) for r in records]
    zone_rects = [(z["rectangle"][0], z["rectangle"][1], z["rectangle"][2], z["rectangle"][3], z["id"])
                  for z in zone_list]
    work = nx * ny + len(footprint_rects) + len(zone_rects)
    if work > budget * _GRID_WORK_FACTOR:
        return {"status": "incomplete", "reason": "budget exhausted", "work": work}
    _, _, fp_counts, _ = _grid_build(xs, ys, footprint_rects, work)
    _, _, zn_counts, _ = _grid_build(xs, ys, zone_rects, work)
    widths = [xs[i + 1] - xs[i] for i in range(nx)]
    global_covered = 0.0
    zoned_covered = 0.0
    zone_totals = {z["id"]: 0.0 for z in zone_list}
    for j in range(ny):
        h = ys[j + 1] - ys[j]
        base = j * nx
        for i in range(nx):
            if fp_counts[base + i] == 0:
                continue
            area = widths[i] * h
            global_covered += area
            if zn_counts[base + i] > 0:
                zoned_covered += area
    # Independent union by identity (duplicates merged) over the same grid.
    members = [None] * (nx * ny)
    for r in records:
        i0, i1 = xs.index(r["rectangle"][0]), xs.index(r["rectangle"][2])
        j0, j1 = ys.index(r["rectangle"][1]), ys.index(r["rectangle"][3])
        for j in range(j0, j1):
            base = j * nx
            for i in range(i0, i1):
                if members[base + i] is None:
                    members[base + i] = set()
                members[base + i].add(r["identity"])
    union_covered = 0.0
    for j in range(ny):
        h = ys[j + 1] - ys[j]
        base = j * nx
        for i in range(nx):
            if members[base + i]:
                union_covered += widths[i] * h
    for z in zone_list:
        zr = z["rectangle"]
        i0, i1 = xs.index(zr[0]), xs.index(zr[2])
        j0, j1 = ys.index(zr[1]), ys.index(zr[3])
        total = 0.0
        for j in range(j0, j1):
            h = ys[j + 1] - ys[j]
            base = j * nx
            for i in range(i0, i1):
                if fp_counts[base + i] > 0:
                    total += widths[i] * h
        zone_totals[z["id"]] = total

    per_zone_sum = sum(z["covered_area"] for z in zone_results)
    checks = {
        # Splitting coverage by zone and re-merging must equal the covered area
        # lying inside the partition on the independent global grid.
        "zone_split_merge": _close(zoned_covered, per_zone_sum),
        # Merging duplicate records by source identity must not change the union.
        "identity_union_vs_stacked_union": _close(union_covered, global_covered),
        "per_zone": {zid: _close(zone_totals[zid], next(z["covered_area"] for z in zone_results if z["zone"] == zid))
                     for zid in zone_totals},
    }
    ok = checks["zone_split_merge"] and checks["identity_union_vs_stacked_union"] \
        and all(checks["per_zone"].values())
    return {
        "status": "complete" if ok else "mismatch",
        "global_covered_area": global_covered,
        "zoned_covered_area": zoned_covered,
        "identity_union_area": union_covered,
        "per_zone_sum": per_zone_sum,
        "checks": checks,
    }
