"""Density-based clustering (DBSCAN semantics) on top of a VectorIndex.

The nearest-neighbor graph is only an approximate candidate channel: a
truncated traversal is never interpreted as "no neighbors". A sample is
declared noise only when its epsilon-neighborhood search fully completed
with insufficient evidence; otherwise it is reported as unconfirmed.

All statistics and memberships are computed against one immutable snapshot
of the index, identified by a version digest, so corrections cannot publish
mixed-epoch results.
"""
import hashlib,heapq
from dataclasses import dataclass

WORK_SET_LIMIT=10000
LABELS=("core","border","noise","unconfirmed")


def _distance(a,b):return sum((x-y)**2 for x,y in zip(a,b))


@dataclass(frozen=True)
class Snapshot:
    ids:tuple
    values:dict
    edges:dict
    version:str


def _digest(ids,values):
    h=hashlib.sha256()
    for i in ids:h.update(repr((i,values[i])).encode("utf-8"))
    return h.hexdigest()[:16]


def snapshot(index):
    ids=tuple(sorted(index.live))
    return Snapshot(ids,dict(index.values),{i:set(index.edges.get(i,())) for i in ids},_digest(ids,index.values))


def neighborhood(snap,identity,epsilon,budget):
    """Bounded epsilon-neighborhood search through the graph.

    Returns neighbors (the live seed is included in its own neighborhood),
    the number of visited graph nodes and whether the traversal covered the
    whole graph.
    """
    if identity not in snap.ids:raise ValueError("unknown identity")
    if epsilon<0 or not isinstance(budget,int) or budget<1:raise ValueError("invalid density parameters")
    limit=epsilon*epsilon;start=snap.values[identity]
    heap=[(0.0,identity)];seen=set();neighbors=[]
    while heap and len(seen)<budget:
        _,node=heapq.heappop(heap)
        if node in seen:continue
        seen.add(node)
        if node in snap.ids and _distance(start,snap.values[node])<=limit:neighbors.append(node)
        for other in snap.edges.get(node,()):
            if other not in seen:heapq.heappush(heap,(_distance(start,snap.values[other]),other))
    return sorted(neighbors),len(seen),len(seen)==len(snap.values)


def _stats(values):
    if not values:return {"count":0}
    ordered=sorted(values);n=len(ordered);mid=n//2
    median=ordered[mid] if n%2 else (ordered[mid-1]+ordered[mid])/2
    return {"count":n,"min":ordered[0],"max":ordered[-1],"mean":sum(ordered)/n,"median":median}


def _histogram(values,edges):
    bins=[0]*len(edges)
    for value in values:
        for j,edge in enumerate(edges):
            if value<=edge:bins[j]+=1;break
        else:bins[-1]+=1  # chained membership can place a pair beyond epsilon
    return {"bin_edges":edges,"counts":bins}


def analyze(index,epsilon,min_samples,budget=10000,focus=None,working_set_limit=WORK_SET_LIMIT):
    if not isinstance(min_samples,int) or min_samples<1:raise ValueError("min_samples must be a positive integer")
    if epsilon<0 or not isinstance(budget,int) or budget<1:raise ValueError("invalid density parameters")
    snap=snapshot(index)
    if len(snap.ids)>working_set_limit:raise ValueError("density working set limit exceeded")
    if focus is not None and focus not in snap.ids:raise ValueError("unknown focus identity")
    limit=epsilon*epsilon

    # Phase 1: per-sample neighborhood evidence. An incomplete search can
    # never prove that a sample has no neighbors.
    evidence={};truncated=0;total_visited=0
    for identity in snap.ids:
        neighbors,visited,complete=neighborhood(snap,identity,epsilon,budget)
        evidence[identity]=(neighbors,complete);total_visited+=visited
        if not complete:truncated+=1

    def is_core(identity):
        neighbors,complete=evidence[identity]
        return complete and len(neighbors)>=min_samples

    # Phase 2: only fully-supported cores seed and expand a cluster.
    labels={};clusters=[];seeds=[]
    for identity in snap.ids:
        if identity in labels or not is_core(identity):continue
        members=set();queue=[identity];seeds.append(identity)
        while queue:
            node=queue.pop()
            if node in members:continue
            members.add(node);labels[node]="core" if is_core(node) else "border"
            if is_core(node):
                for other in evidence[node][0]:
                    if other not in members:queue.append(other)
        clusters.append(members)

    # Phase 3: unsupported leftovers are noise only with complete evidence.
    for identity in snap.ids:
        if identity not in labels:
            labels[identity]="noise" if evidence[identity][1] else "unconfirmed"

    edges=[round(limit*(j+1)/4,12) for j in range(4)]
    cluster_reports=[]
    for cid,members in enumerate(clusters):
        pair_distances=[];neighbor_distances=[];ordered=sorted(members)
        for pos,a in enumerate(ordered):
            va=snap.values[a];within=[]
            for b in ordered[pos+1:]:
                d=_distance(va,snap.values[b]);pair_distances.append(d)
                if d<=limit:within.append(d)
            if within:neighbor_distances.append(min(within))
        cluster_reports.append({
            "cluster_id":cid,
            "seed":seeds[cid],
            "size":len(members),
            "members":ordered,
            "confirmed":all(evidence[i][1] for i in members),
            "distance_distribution":{
                "squared_pairwise":_stats(pair_distances),
                "squared_nearest_within_epsilon":_stats(neighbor_distances),
                "histogram":_histogram(pair_distances,edges),
            },
        })

    assignments=[]
    for identity in snap.ids:
        neighbors,complete=evidence[identity]
        cid=next((c for c,members in enumerate(clusters) if identity in members),None)
        nearest=sorted(_distance(snap.values[identity],snap.values[o]) for o in neighbors if o!=identity)
        assignments.append({
            "id":identity,
            "label":labels[identity],
            "cluster_id":cid,
            "neighbors_within_epsilon":len(neighbors)-1,
            "neighborhood_complete":complete,
            "nearest_squared_distance":nearest[0] if nearest else None,
        })

    result={
        "parameters":{"epsilon":epsilon,"min_samples":min_samples,"budget":budget},
        "snapshot":{"version":snap.version,"sample_count":len(snap.ids),"ids":list(snap.ids)},
        "exact":truncated==0,
        "truncated_searches":truncated,
        "visited_total":total_visited,
        "working_set_limit":working_set_limit,
        "clusters":cluster_reports,
        "assignments":assignments,
        "label_counts":{name:sum(1 for v in labels.values() if v==name) for name in LABELS},
        "criteria":{
            "core":"neighborhood search completed and at least min_samples samples (including itself) lie within epsilon",
            "border":"not a core but reachable within epsilon from a confirmed core",
            "noise":"neighborhood search completed with fewer than min_samples samples and no confirmed core reachable",
            "unconfirmed":"neighborhood search was truncated by the traversal budget, so noise cannot be proven",
        },
    }
    if focus is not None:
        result["focus"]={**next(a for a in assignments if a["id"]==focus),
                         "neighbor_ids":evidence[focus][0]}
    return result
