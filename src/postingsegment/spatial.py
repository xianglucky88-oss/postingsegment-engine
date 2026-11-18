import math
from dataclasses import dataclass

def box(v):
    if len(v)!=4 or any(not math.isfinite(x) for x in v) or v[0]>v[2] or v[1]>v[3]:raise ValueError("invalid rectangle")
    return tuple(v)
def intersects(a,b):return a[0]<=b[2] and b[0]<=a[2] and a[1]<=b[3] and b[1]<=a[3]
def union(vs):return (min(v[0] for v in vs),min(v[1] for v in vs),max(v[2] for v in vs),max(v[3] for v in vs))
@dataclass(frozen=True)
class Node:
    bounds:tuple
    children:tuple=()
    identity:object=None
class SpatialIndex:
    def __init__(self,items):
        if len(items)>10000:raise ValueError("geometry limit")
        self.items={str(i):box(v) for i,v in items.items()}
        def build(nodes):
            if not nodes:return None
            if len(nodes)==1:return nodes[0]
            nodes=sorted(nodes,key=lambda x:(x.bounds[0]+x.bounds[2],str(x.identity)));m=len(nodes)//2
            left,right=build(nodes[:m]),build(nodes[m:])
            return Node(union([left.bounds,right.bounds]),(left,right))
        self.root=build([Node(v,identity=i) for i,v in self.items.items()])
    def query(self,rectangle):
        q=box(rectangle);stack=[self.root] if self.root else [];found=[]
        while stack:
            n=stack.pop()
            if not intersects(n.bounds,q):continue
            if n.children:stack.extend(n.children)
            else:found.append(n.identity)
        return sorted(found)
