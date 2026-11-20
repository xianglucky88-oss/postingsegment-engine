import tempfile
from . import datasets, segments, vectors, paths, spatial, morphology

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

def dataset_call(d):
    """Apply a list of versioned ingest requests in one disposable dataset.

    The operation is a per-request computation: it renders batches on a fresh
    temporary directory, takes a pinned snapshot after the requested batch so
    old-reader isolation can be observed, and reports the final version table
    next to query matches.  No caller-supplied paths are accepted.
    """
    batches=d.get('batches')
    if not isinstance(batches,list) or not batches:raise ValueError('batches must be a non-empty list')
    if len(batches)>16:raise ValueError('at most 16 batches per request')
    queries=d.get('queries',[])
    if not isinstance(queries,list) or len(queries)>32:raise ValueError('queries must be a list of at most 32 entries')
    snapshot_after=d.get('snapshot_after')
    if snapshot_after is not None and (not isinstance(snapshot_after,int) or not 0<=snapshot_after<=len(batches)):raise ValueError('snapshot_after must index the batches')
    with tempfile.TemporaryDirectory() as directory:
        dataset=datasets.Dataset(directory)
        old=dataset.reader() if snapshot_after==0 else None;results=[]
        for position,request in enumerate(batches):
            results.append(dataset.ingest(request))
            if position+1==snapshot_after:old=dataset.reader()
        pinned=old.query_all(queries) if old is not None else None
        merged=dataset.merge() if d.get('merge') else None
        reclaimed=dataset.reclaim() if d.get('reclaim') else None
        current=dataset.reader()
        payload={
            'batches':results,
            'generation':current.generation,
            'versions':current.versions(),
            'queries':current.query_all(queries),
        }
        if pinned is not None:payload['pinned']={'generation':old.generation,'queries':pinned}
        if merged is not None:payload['merge']=merged
        if reclaimed is not None:payload['reclaim']=reclaimed
        current.close()
        if old is not None:old.close()
        return payload

OPERATIONS={'segments':segment_call,'datasets':dataset_call,'vectors':vector_call,
 'paths':lambda d:{'ends':paths.reachable(d['edges'],d['start'],d['expression'])},
 'spatial':lambda d:{'matches':spatial.SpatialIndex(d['items']).query(d['rectangle'])},
 'morphology':lambda d:{'analyses':morphology.Transducer(d['arcs'],d['start'],d['finals']).analyze(d['text'])}}
EXAMPLES={
 'segments':{'documents':{'a':'red oak tree','b':'oak red tree'},'query':'red oak','phrase':True},
 'datasets':{
  'batches':[
   {'source':'cms','batch':'batch-initial',
    'mapping':{'id':'key','version':'rev','revision':'edit','retracted':'withdrawn','text':'body'},
    'records':[{'key':'a','rev':1,'body':'red oak tree'},{'key':'b','rev':1,'body':'oak red tree'}]},
   {'source':'cms','batch':'batch-revise',
    'records':[{'key':'b','rev':2,'edit':1,'body':'red oak'},{'key':'a','rev':2,'edit':1,'withdrawn':True}]}],
  'queries':[{'text':'red oak','phrase':True},{'text':'oak'}],
  'snapshot_after':1,'merge':True},
 'vectors':{'vectors':{'a':[0,0],'b':[1,1],'c':[5,5]},'query':[.8,1],'k':2},
 'paths':{'edges':[['a','p','b'],['b','p','c']],'start':'a','expression':{'star':{'label':'p'}}},
 'spatial':{'items':{'a':[0,0,1,1],'b':[3,3,4,4]},'rectangle':[1,1,2,2]},
 'morphology':{'arcs':[[0,1,'cat','cat',0],[1,2,'s','+PL',.2]],'start':0,'finals':[1,2],'text':'cats'},
}
