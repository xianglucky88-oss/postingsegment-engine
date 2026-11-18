import json,os,re,tempfile,uuid
from pathlib import Path

def atomic(path,value):
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.write-')
    try:
        with os.fdopen(fd,'w') as f:json.dump(value,f,sort_keys=True);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def tokens(text):return re.findall(r'\w+',text.casefold())
class Index:
    def __init__(self,path):
        self.path=Path(path);self.path.mkdir(parents=True,exist_ok=True);self.manifest=self.path/'segments.json'
        if not self.manifest.exists():atomic(self.manifest,[])
        self.pending={}
    def add(self,identity,text):
        if not isinstance(identity,str) or not isinstance(text,str) or len(text)>100000:raise ValueError("invalid document")
        self.pending[identity]=tokens(text)
    def delete(self,identity):self.pending[identity]=None
    def commit(self):
        if not self.pending:return
        current=json.loads(self.manifest.read_text());name='segment-'+uuid.uuid4().hex+'.json'
        atomic(self.path/name,self.pending);atomic(self.manifest,current+[name]);self.pending={}
    def reader(self):
        docs={}
        for name in json.loads(self.manifest.read_text()):
            if not re.fullmatch(r'segment-[0-9a-f]{32}\.json',name):raise ValueError("invalid segment identity")
            segment=json.loads((self.path/name).read_text());docs.update(segment)
        return Reader({k:v for k,v in docs.items() if v is not None})
class Reader:
    def __init__(self,docs):
        self.postings={}
        for identity,words in docs.items():
            for i,word in enumerate(words):self.postings.setdefault(word,{}).setdefault(identity,[]).append(i)
    def query(self,text,phrase=False):
        terms=tokens(text)
        if not terms:return []
        ids=set(self.postings.get(terms[0],{}))
        for t in terms[1:]:ids&=self.postings.get(t,{}).keys()
        if phrase:
            ids={i for i in ids if any(all(start+k in self.postings[t][i] for k,t in enumerate(terms)) for start in self.postings[terms[0]][i])}
        return sorted(ids)
