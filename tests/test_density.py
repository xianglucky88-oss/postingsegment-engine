import itertools,random
import pytest
from postingsegment import vectors,density
from postingsegment import api


def d2(a,b):return sum((x-y)**2 for x,y in zip(a,b))


def reference_dbscan(values,epsilon,min_samples):
    """Independent O(n^2) DBSCAN over exact full neighborhoods."""
    ids=sorted(values);limit=epsilon*epsilon
    nb={i:[j for j in ids if d2(values[i],values[j])<=limit] for i in ids}
    core={i for i in ids if len(nb[i])>=min_samples}
    labels={};clusters=[]
    for seed in ids:
        if seed not in core or seed in labels:continue
        members=set();queue=[seed]
        while queue:
            node=queue.pop()
            if node in members:continue
            members.add(node);labels[node]=0
            if node in core:queue.extend(o for o in nb[node] if o not in members)
        clusters.append(members)
    roles={}
    for i in ids:
        if i in labels:
            roles[i]="core" if i in core else "border"
        else:roles[i]="noise"
    return {frozenset(c) for c in clusters},roles


def build(values,m=4):
    idx=vectors.VectorIndex(len(next(iter(values.values()))),m)
    for i,v in values.items():idx.add(i,v)
    return idx


# a,b,c form one tight cluster; d,e,f another; g sits within epsilon of both
# groups (a real bridge core); h is fully isolated.
SAMPLE={'a':[0,0],'b':[0.1,0.1],'c':[0.2,0.0],
        'd':[2.0,0],'e':[2.1,0.1],'f':[2.0,0.2],
        'g':[1.1,0.0],'h':[9,9]}


def test_membership_sets_match_independent_reference():
    idx=build(SAMPLE)
    # Tight epsilon: the bridge and the isolated point both lack support.
    result=density.analyze(idx,epsilon=0.5,min_samples=2)
    assert result["exact"] and result["truncated_searches"]==0
    got={frozenset(c["members"]) for c in result["clusters"]}
    expect_clusters,expect_roles=reference_dbscan(SAMPLE,0.5,2)
    # Acceptance compares member sets, never cluster numbers.
    assert got==expect_clusters
    roles={a["id"]:a["label"] for a in result["assignments"]}
    assert roles==expect_roles
    # The two dense groups stay separate; g and h are provable noise.
    assert len(got)==2 and result["label_counts"]["noise"]==2
    assert roles["g"]=="noise" and roles["h"]=="noise"
    assert all(a["neighborhood_complete"] for a in result["assignments"])


def test_bridge_deletion_splits_one_cluster_into_two():
    idx=build(SAMPLE)
    joined=density.analyze(idx,1.2,2)
    assert len(joined["clusters"])==1  # core bridge g chains the two groups
    idx.delete("g")
    split=density.analyze(idx,1.2,2)
    assert len(split["clusters"])==2
    members={frozenset(c["members"]) for c in split["clusters"]}
    ref_clusters,_=reference_dbscan({k:v for k,v in SAMPLE.items() if k!="g"},1.2,2)
    assert members==ref_clusters


def test_duplicate_vectors_share_neighborhood_evidence():
    values=dict(SAMPLE);values["a2"]=[0,0]  # exact duplicate of a
    idx=build(values)
    result=density.analyze(idx,1.2,2)
    roles={a["id"]:a["label"] for a in result["assignments"]}
    _,expect=reference_dbscan(values,1.2,2)
    assert roles==expect and roles["a"]=="core" and roles["a2"]=="core"
    a_row=next(a for a in result["assignments"] if a["id"]=="a")
    assert a_row["nearest_squared_distance"]==0.0


def test_failed_correction_rolls_back_and_publishes_nothing_mixed():
    idx=build(SAMPLE);before=density.snapshot(idx)
    with pytest.raises(ValueError):
        idx.apply([{"op":"replace","id":"a","vector":[0.05,0.05]},
                   {"op":"replace","id":"missing","vector":[1,1]}])
    after=density.snapshot(idx)
    assert after.version==before.version and after.ids==before.ids
    result=density.analyze(idx,1.2,2)
    assert result["snapshot"]["version"]==before.version


def test_successful_correction_can_merge_or_split_clusters():
    idx=build(SAMPLE)
    # Moving the isolated h next to group a..c grows that cluster.
    idx.apply([{"op":"replace","id":"h","vector":[0.05,0.05]}])
    result=density.analyze(idx,1.2,2)
    ref_clusters,_=reference_dbscan({**SAMPLE,"h":[0.05,0.05]},1.2,2)
    assert {frozenset(c["members"]) for c in result["clusters"]}==ref_clusters


def test_snapshot_pins_membership_and_statistics_to_one_version():
    idx=build(SAMPLE)
    first=density.analyze(idx,1.2,2)
    first_version=first["snapshot"]["version"]
    idx.replace("h",[0.05,0.05])  # mutate after the analysis was produced
    assert first["snapshot"]["version"]==first_version
    frozen_members=[tuple(c["members"]) for c in first["clusters"]]
    again=density.analyze(idx,1.2,2)
    assert again["snapshot"]["version"]!=first_version
    assert [tuple(c["members"]) for c in again["clusters"]]!=frozen_members


def test_truncated_search_is_unconfirmed_never_false_noise():
    rng=random.Random(11)
    # Many tightly packed points: a budget of one only reaches the seed.
    values={str(i):[rng.random()*0.01,rng.random()*0.01] for i in range(25)}
    idx=build(values)
    result=density.analyze(idx,epsilon=1.0,min_samples=3,budget=1)
    assert not result["exact"] and result["truncated_searches"]>0
    assert result["label_counts"]["noise"]==0  # incomplete evidence is not noise
    assert all(a["label"]=="unconfirmed" for a in result["assignments"])
    # With a full budget the same data yields one confirmed dense cluster.
    full=density.analyze(idx,1.0,3)
    assert full["exact"] and len(full["clusters"])==1
    assert full["clusters"][0]["confirmed"]
    assert set(full["clusters"][0]["members"])==set(values)


def test_dense_data_recall_and_working_cost_reported():
    rng=random.Random(13)
    values={str(i):[rng.random()*0.05,rng.random()*0.05] for i in range(60)}
    values["far"]=[20,20]
    idx=build(values)
    tight=density.analyze(idx,epsilon=0.5,min_samples=4,budget=10)
    # Fragmented, conservative result: every reported member is still a true
    # member of the exact dense cluster (no false positives).
    exact=density.analyze(idx,0.5,4)
    true_core_cluster=next(c for c in exact["clusters"] if "0" in c["members"])
    for c in tight["clusters"]:
        assert set(c["members"])<=set(true_core_cluster["members"])
    assert tight["visited_total"]>=len(values)-1
    with pytest.raises(ValueError):
        density.analyze(idx,0.5,4,working_set_limit=10)


def test_cluster_distance_distribution_and_focus_output():
    idx=build(SAMPLE)
    result=density.analyze(idx,1.2,2,focus="g")
    cluster=result["clusters"][0]
    stats=cluster["distance_distribution"]["squared_pairwise"]
    assert stats["count"]==cluster["size"]*(cluster["size"]-1)//2
    assert stats["min"]<=stats["median"]<=stats["max"]
    hist=cluster["distance_distribution"]["histogram"]
    assert sum(hist["counts"])==stats["count"]
    assert result["focus"]["id"]=="g" and "neighbor_ids" in result["focus"]


def test_invalid_parameters_rejected():
    idx=build(SAMPLE)
    with pytest.raises(ValueError):density.analyze(idx,-1,2)
    with pytest.raises(ValueError):density.analyze(idx,1.0,0)
    with pytest.raises(ValueError):density.analyze(idx,1.0,2,budget=0)
    with pytest.raises(ValueError):density.analyze(idx,1.0,2,focus="nope")


def test_density_operation_via_service_api_and_vectors_still_available():
    request=dict(api.EXAMPLES["density"])
    out=api.OPERATIONS["density"](request)
    assert {c["size"] for c in out["clusters"]}>={2,3}
    assert out["snapshot"]["sample_count"]==6
    # Original vector query is unchanged and still answers top-k neighbors.
    query=api.OPERATIONS["vectors"](api.EXAMPLES["vectors"])
    assert [n["id"] for n in query["neighbors"]]==["b","a"]
    with pytest.raises(ValueError):api.OPERATIONS["density"]({"vectors":{},"epsilon":1,"min_samples":2})


def test_large_random_index_agrees_with_reference_membership():
    rng=random.Random(23)
    values={str(i):[rng.gauss(0 if i%2 else 8,0.4),rng.gauss(0 if i%2 else 8,0.4)] for i in range(40)}
    idx=build(values)
    result=density.analyze(idx,epsilon=1.5,min_samples=3)
    assert result["exact"]
    got={frozenset(c["members"]) for c in result["clusters"]}
    expect,roles=reference_dbscan(values,1.5,3)
    assert got==expect
    assert {a["id"]:a["label"] for a in result["assignments"]}==roles
