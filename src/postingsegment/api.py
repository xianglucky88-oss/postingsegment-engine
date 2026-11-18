import tempfile
from . import segments,vectors,paths,spatial,morphology

def segment_call(d):
    with tempfile.TemporaryDirectory() as directory:
        index=segments.Index(directory)
        for identity,text in d['documents'].items():index.add(identity,text)
        index.commit();return {'matches':index.reader().query(d['query'],d.get('phrase',False))}
def vector_call(d):
    idx=vectors.VectorIndex(len(d['query']))
    for identity,value in d['vectors'].items():idx.add(identity,value)
    for identity in d.get('deleted',[]):idx.delete(identity)
    return idx.query(d['query'],d.get('k',3),d.get('budget',10000))
OPERATIONS={'segments':segment_call,'vectors':vector_call,'paths':lambda d:{'ends':paths.reachable(d['edges'],d['start'],d['expression'])},
 'spatial':lambda d:{'matches':spatial.SpatialIndex(d['items']).query(d['rectangle'])},
 'morphology':lambda d:{'analyses':morphology.Transducer(d['arcs'],d['start'],d['finals']).analyze(d['text'])}}
EXAMPLES={
 'segments':{'documents':{'a':'red oak tree','b':'oak red tree'},'query':'red oak','phrase':True},
 'vectors':{'vectors':{'a':[0,0],'b':[1,1],'c':[5,5]},'query':[.8,1],'k':2},
 'paths':{'edges':[['a','p','b'],['b','p','c']],'start':'a','expression':{'star':{'label':'p'}}},
 'spatial':{'items':{'a':[0,0,1,1],'b':[3,3,4,4]},'rectangle':[1,1,2,2]},
 'morphology':{'arcs':[[0,1,'cat','cat',0],[1,2,'s','+PL',.2]],'start':0,'finals':[1,2],'text':'cats'},
}
