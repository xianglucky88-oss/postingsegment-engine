import math,itertools,random
import pytest
from postingsegment import segments,vectors,paths,spatial,morphology

def test_index_persistence_and_old_reader(tmp_path):
    i=segments.Index(tmp_path);i.add('a','red oak tree');i.add('b','oak red tree');i.commit();old=i.reader()
    assert old.query('red oak',phrase=True)==['a'] and old.query('oak red')==['a','b']
    i.delete('a');i.add('b','red oak');i.commit()
    assert old.query('red oak',True)==['a']
    assert segments.Index(tmp_path).reader().query('red oak',True)==['b']
    assert i.reader().query('')==[]
def test_repeated_word_positions(tmp_path):
    i=segments.Index(tmp_path);i.add('a','go go now');i.add('b','go now');i.commit()
    assert i.reader().query('go go',True)==['a']
def test_vector_search_against_bruteforce_and_deletion():
    rng=random.Random(6);values={str(i):[rng.random(),rng.random()] for i in range(30)};idx=vectors.VectorIndex(2,4)
    for name,v in values.items():idx.add(name,v)
    q=[.3,.7];expected=sorted(values,key=lambda n:(sum((x-y)**2 for x,y in zip(q,values[n])),n))[:4]
    r=idx.query(q,4,100);assert [x['id'] for x in r['neighbors']]==expected and r['complete']
    idx.delete(expected[0]);assert expected[0] not in [x['id'] for x in idx.query(q,4,100)['neighbors']]
    with pytest.raises(ValueError):idx.add('wrong',[float('nan'),1])
def test_product_graph_paths_and_epsilon():
    edges=[['a','p','b'],['b','p','c'],['c','q','d'],['c','p','a']]
    assert paths.reachable(edges,'a',{'star':{'label':'p'}})==['a','b','c']
    assert paths.reachable(edges,'a',{'seq':[{'star':{'label':'p'}},{'label':'q'}]})==['d']
    assert paths.reachable(edges,'a',{'seq':[]})==['a']
    with pytest.raises(ValueError):paths.reachable(edges,'a',{'star':{'label':'p'}},budget=1)
def test_spatial_boundaries_against_scan():
    rng=random.Random(8);items={}
    for i in range(100):
        x,y=rng.randrange(20),rng.randrange(20);items[str(i)]=[x,y,x+rng.randrange(4),y+rng.randrange(4)]
    tree=spatial.SpatialIndex(items)
    for q in [[0,0,3,3],[5,5,5,5],[0,0,30,30]]:
        expected=sorted(i for i,b in items.items() if b[0]<=q[2] and q[0]<=b[2] and b[1]<=q[3] and q[1]<=b[3])
        assert tree.query(q)==expected
    with pytest.raises(ValueError):spatial.SpatialIndex({'bad':[1,0,0,1]})
def test_morphology_ambiguity_and_epsilon_cycle():
    t=morphology.Transducer([[0,1,'a','A',1],[0,2,'a','B',.5],[2,1,'','!',0]],0,[1])
    assert t.analyze('a')==[{'analysis':'B!','weight':.5},{'analysis':'A','weight':1.0}]
    assert t.analyze('z')==[]
    with pytest.raises(ValueError):morphology.Transducer([[0,0,'','x',0]],0,[0])
