import tempfile
from . import segments,vectors,paths,spatial,morphology,density

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
def density_call(d):
    if not d.get('vectors'):raise ValueError("at least one vector is required")
    idx=vectors.VectorIndex(len(next(iter(d['vectors'].values()))))
    for identity,value in d['vectors'].items():idx.add(identity,value)
    # Corrections run as one batch: a failed op rolls the whole index back,
    # so the analysis snapshot never mixes sample versions.
    if d.get('updates'):idx.apply(d['updates'])
    return density.analyze(idx,d['epsilon'],d['min_samples'],d.get('budget',10000),d.get('focus'))
OPERATIONS={'segments':segment_call,'vectors':vector_call,
 'density':density_call,
 'paths':lambda d:{'ends':paths.reachable(d['edges'],d['start'],d['expression'])},
 'spatial':lambda d:{'matches':spatial.SpatialIndex(d['items']).query(d['rectangle'])},
 'morphology':lambda d:{'analyses':morphology.Transducer(d['arcs'],d['start'],d['finals']).analyze(d['text'])}}
EXAMPLES={
 'segments':{'documents':{'a':'red oak tree','b':'oak red tree'},'query':'red oak','phrase':True},
 'vectors':{'vectors':{'a':[0,0],'b':[1,1],'c':[5,5]},'query':[.8,1],'k':2},
 'density':{'vectors':{'a':[0,0],'b':[0.1,0.1],'c':[0.2,0.0],'d':[6,6],'e':[6.2,6.1],'f':[3,3]},
            'epsilon':1.0,'min_samples':2,'focus':'f'},
 'paths':{'edges':[['a','p','b'],['b','p','c']],'start':'a','expression':{'star':{'label':'p'}}},
 'spatial':{'items':{'a':[0,0,1,1],'b':[3,3,4,4]},'rectangle':[1,1,2,2]},
 'morphology':{'arcs':[[0,1,'cat','cat',0],[1,2,'s','+PL',.2]],'start':0,'finals':[1,2],'text':'cats'},
}
