import heapq,math
class VectorIndex:
    def __init__(self,dimension,neighbors=4):
        if not isinstance(dimension,int) or not 1<=dimension<=1024 or not 1<=neighbors<=32:raise ValueError("invalid vector configuration")
        self.dimension=dimension;self.m=neighbors;self.values={};self.edges={};self.live=set()
    def vector(self,values):
        v=tuple(float(x) for x in values)
        if len(v)!=self.dimension or any(not math.isfinite(x) for x in v):raise ValueError("invalid vector")
        return v
    @staticmethod
    def distance(a,b):return sum((x-y)**2 for x,y in zip(a,b))
    def add(self,identity,values):
        if identity in self.values or len(self.values)>=10000:raise ValueError("duplicate identity or index full")
        v=self.vector(values);nearest=sorted(self.values,key=lambda i:(self.distance(v,self.values[i]),i))[:self.m]
        self.values[identity]=v;self.edges[identity]=set(nearest);self.live.add(identity)
        for i in nearest:
            self.edges[i].add(identity)
            # Keep reciprocal links; this small graph prioritizes reachability.
    def delete(self,identity):self.live.discard(identity)
    def query(self,values,k=3,budget=10000):
        q=self.vector(values)
        if not isinstance(k,int) or k<1 or not isinstance(budget,int) or budget<1:raise ValueError("invalid query budget")
        if not self.values:return {"neighbors":[],"visited":0,"complete":True}
        start=min(self.values);heap=[(self.distance(q,self.values[start]),start)];seen=set();found=[]
        while heap and len(seen)<budget:
            distance,node=heapq.heappop(heap)
            if node in seen:continue
            seen.add(node)
            if node in self.live:found.append((distance,node))
            for other in self.edges[node]:
                if other not in seen:heapq.heappush(heap,(self.distance(q,self.values[other]),other))
        return {"neighbors":[{"id":i,"distance_squared":v} for v,i in sorted(found)[:k]],"visited":len(seen),"complete":len(seen)==len(self.values)}
