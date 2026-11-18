import heapq,math
from collections import defaultdict
class Transducer:
    def __init__(self,arcs,start,finals):
        self.arcs=defaultdict(list);self.start=start;self.finals=set(finals);epsilon=defaultdict(list)
        if len(arcs)>1000:raise ValueError("arc limit")
        states={start}|self.finals
        for src,dst,label,out,cost in arcs:
            if not isinstance(label,str) or not isinstance(out,str) or not math.isfinite(cost) or cost<0:raise ValueError("invalid arc")
            self.arcs[src].append((dst,label,out,float(cost)));states|={src,dst}
            if not label:epsilon[src].append(dst)
        color={}
        def visit(n):
            if color.get(n)==1:raise ValueError("epsilon cycles are unsupported")
            if color.get(n)==2:return
            color[n]=1
            for nxt in epsilon[n]:visit(nxt)
            color[n]=2
        for state in states:visit(state)
    def analyze(self,text,k=8,budget=10000):
        if len(text)>10000 or not 1<=k<=100:raise ValueError("invalid analysis request")
        heap=[(0.0,0,self.start,0,'')];serial=1;results=[];seen=set();work=0
        while heap and len(results)<k:
            cost,_,state,pos,out=heapq.heappop(heap);key=(state,pos,out,cost)
            if key in seen:continue
            seen.add(key);work+=1
            if work>budget:raise ValueError("analysis budget exceeded")
            if state in self.finals and pos==len(text):results.append({"analysis":out,"weight":cost})
            for dst,label,emitted,weight in self.arcs[state]:
                if text.startswith(label,pos):heapq.heappush(heap,(cost+weight,serial,dst,pos+len(label),out+emitted));serial+=1
        return results
