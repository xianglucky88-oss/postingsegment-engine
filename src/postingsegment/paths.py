from collections import defaultdict,deque
class Automaton:
    def __init__(self,expression):
        self.edges=defaultdict(list);self.next=0;self.nodes=0
        self.start,self.end=self.build(expression)
    def fresh(self):i=self.next;self.next+=1;return i
    def build(self,expr,depth=0):
        self.nodes+=1
        if self.nodes>256 or depth>32 or not isinstance(expr,dict) or len(expr)!=1:raise ValueError("invalid path expression")
        a,b=self.fresh(),self.fresh()
        if 'label' in expr:
            if not isinstance(expr['label'],str):raise ValueError("label must be string")
            self.edges[a].append((expr['label'],b))
        elif 'seq' in expr:
            cur=a
            for sub in expr['seq']:
                x,y=self.build(sub,depth+1);self.edges[cur].append((None,x));cur=y
            self.edges[cur].append((None,b))
        elif 'alt' in expr:
            for sub in expr['alt']:
                x,y=self.build(sub,depth+1);self.edges[a].append((None,x));self.edges[y].append((None,b))
        elif 'star' in expr:
            x,y=self.build(expr['star'],depth+1)
            self.edges[a].extend([(None,b),(None,x)]);self.edges[y].extend([(None,x),(None,b)])
        else:raise ValueError("unsupported path expression")
        return a,b

def reachable(edges,start,expression,budget=100000):
    graph=defaultdict(list)
    for a,label,b in edges:graph[a].append((label,b))
    nfa=Automaton(expression);queue=deque([(start,nfa.start)]);seen=set();result=set()
    while queue:
        node,state=queue.popleft()
        if (node,state) in seen:continue
        seen.add((node,state))
        if len(seen)>budget:raise ValueError("path search budget exceeded")
        if state==nfa.end:result.add(node)
        for label,target in nfa.edges[state]:
            if label is None:queue.append((node,target))
            else:queue.extend((b,target) for name,b in graph[node] if name==label)
    return sorted(result)
