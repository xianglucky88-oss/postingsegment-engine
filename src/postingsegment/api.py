import tempfile
from . import segments,vectors,paths,spatial,morphology,coverage

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
 'morphology':lambda d:{'analyses':morphology.Transducer(d['arcs'],d['start'],d['finals']).analyze(d['text'])},
 'coverage':lambda d:coverage.analyze_coverage(d['footprints'],d['zones'],d.get('reference_units'),
   threshold=d.get('threshold',1.0),budget=d.get('budget',1000000))}
EXAMPLES={
 'segments':{'documents':{'a':'red oak tree','b':'oak red tree'},'query':'red oak','phrase':True},
 'vectors':{'vectors':{'a':[0,0],'b':[1,1],'c':[5,5]},'query':[.8,1],'k':2},
 'paths':{'edges':[['a','p','b'],['b','p','c']],'start':'a','expression':{'star':{'label':'p'}}},
 'spatial':{'items':{'a':[0,0,1,1],'b':[3,3,4,4]},'rectangle':[1,1,2,2]},
 'morphology':{'arcs':[[0,1,'cat','cat',0],[1,2,'s','+PL',.2]],'start':0,'finals':[1,2],'text':'cats'},
 'coverage':{'footprints':[{'id':'f1','identity':'obs-a','rectangle':[0,0,2,2]},
   {'id':'f2','identity':'obs-a','rectangle':[1,1,3,3]},
   {'id':'f3','identity':'obs-b','rectangle':[8,8,9,9]}],
  'zones':[{'id':'north','rectangle':[0,0,5,5]},{'id':'south','rectangle':[0,5,5,10]}],
  'reference_units':[{'id':'u1','rectangle':[0,0,1,1],'weight':2.0,'stratum':'urban'},
   {'id':'u2','rectangle':[4,4,5,5],'weight':1.0,'stratum':'rural'},
   {'id':'u3','rectangle':[0,5,1,6],'weight':None,'stratum':'rural'}]},
}
