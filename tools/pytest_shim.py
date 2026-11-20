"""Minimal pytest shim for environments without pip (supports this test suite)."""
import contextlib,traceback,sys,types,tempfile,pathlib


class Raises:
    def __init__(self,exc):self.exc=exc
    def __enter__(self):return self
    def __exit__(self,etype,value,tb):
        if etype is None:raise AssertionError(f"expected {self.exc.__name__}")
        return issubclass(etype,self.exc)


pytest=types.ModuleType("pytest")
pytest.raises=Raises
sys.modules["pytest"]=pytest


def fixture(fn):
    def wrapper(*args,**kwargs):
        gen=fn()
        try:value=next(gen)
        except StopIteration:return
        return value
    wrapper.__wrapped__=fn;return wrapper
pytest.fixture=fixture


@pytest.fixture
def tmp_path():
    with tempfile.TemporaryDirectory() as d:yield pathlib.Path(d)


def run(modname,path):
    mod=types.ModuleType(modname);mod.__dict__["pytest"]=pytest
    sys.modules[modname]=mod
    code=compile(open(path).read(),path,"exec")
    exec(code,mod.__dict__)
    failures=0;tests=[(n,v) for n,v in vars(mod).items() if n.startswith("test_") and callable(v)]
    for name,fn in tests:
        try:
            import inspect
            if "tmp_path" in inspect.signature(fn).parameters:
                with tempfile.TemporaryDirectory() as d:fn(pathlib.Path(d))
            else:fn()
            print(f"PASS {name}")
        except Exception:
            failures+=1;print(f"FAIL {name}");traceback.print_exc()
    print(f"{len(tests)-failures}/{len(tests)} passed in {modname}")
    return failures


if __name__=="__main__":
    sys.path.insert(0,"src");sys.path.insert(0,"tests")
    rc=0
    for modname,path in [("test_density","tests/test_density.py"),("test_core","tests/test_core.py")]:
        rc+=run(modname,path)
    sys.exit(1 if rc else 0)
