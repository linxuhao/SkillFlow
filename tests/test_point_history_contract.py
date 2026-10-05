from pathlib import Path
import pytest
from skillflow import citations
from skillflow.read_tools import unified_read
from skillflow.strict_patch import apply_code_patch

RUN = 'point-history-contract'
@pytest.fixture
def root(tmp_path):
    citations.forget_run(RUN)
    yield tmp_path
    citations.forget_run(RUN)

def read(root):
    return unified_read({'working_tree': [('repo', str(root))], 'named': {}, 'allowed': set()}, 'f.txt', run_id=RUN)['citation']['sha']
def apply(root, sha, **kw):
    return apply_code_patch('', root, references=[{'file':'f.txt','sha':sha, **kw}], run_id=RUN)
def point(root, sha, line, col=0, new='X\n'):
    r = apply(root, sha, from_line=line,to_line=line,from_col=col,to_col=col,new_text=new)
    return r['spans'][0]['sha'] if 'spans' in r else sha

@pytest.mark.parametrize('new',['changed',''])
def test_point_does_not_follow_replaced_left_neighbor(root,new):
    (root/'f.txt').write_text('left\nright\ntail\n')
    sha = read(root)
    p = point(root,sha,2)
    assert apply(root,sha,from_line=1,to_line=1,new_text=new)['applied']
    before=(root/'f.txt').read_bytes()
    r=apply(root,p,new_text='X\n')
    assert not r['applied'], r
    assert 'neighbor' in r['error'] and 'f.txt' in r['error']
    assert (root/'f.txt').read_bytes()==before

@pytest.mark.parametrize('limit,expected',[(2,False),(20,True)])
def test_history_depth_is_observable_to_mutation_caller(root,monkeypatch,limit,expected):
    monkeypatch.setattr(citations,'MAX_GENERATIONS_PER_FILE',limit)
    (root/'f.txt').write_text('top\nkeep\nbottom\n')
    old=read(root)
    for i in range(3):
        sha=read(root)
        r=apply(root,sha,from_line=1,to_line=1,new_text=f'top{i}')
        assert r['applied'],r
        assert r['remap_history'][0]['max_generations']==limit
    assert r['remap_history'][0]['retained_generations'] <= limit
    assert ('warning' in r['remap_history'][0]) is (not expected)
    r=apply(root,old,from_line=2,to_line=2,new_text='KEPT')
    assert r['applied'] is expected,r
    if not expected:
        assert 'history' in r['error'] and 'reread' in r['error']

@pytest.mark.parametrize('text,line,col,new',[
    ('α🙂\nβ\n',1,2,'!'),
    ('α🙂\nβ\n',1,0,'prefix '),
    ('one\n\ntwo\n',2,0,'blank\n'),
    ('one\ntwo\n',2,3,'!'),
    ('one\ntwo\n',2,0,'before\n'),
    ('\n',1,0,'empty\n'),
])
def test_legal_points_and_unicode_newline_boundaries(root,text,line,col,new):
    (root/'f.txt').write_text(text)
    old=read(root)
    p=point(root,old,line,col,new)
    r=apply(root,p,new_text=new)
    assert r['applied'],r
    lines=text.splitlines(keepends=True)
    offset=sum(map(len,lines[:line-1]))+col
    assert (root/'f.txt').read_text()==text[:offset]+new+text[offset:]


def test_many_sequential_ranges_and_points_from_one_read(root):
    (root/'f.txt').write_text('above\nleft\nanchor\nright\nbelow\nlast\n')
    old=read(root)
    p=point(root,old,3,0)
    for line,new in [(1,'long\nabove'),(5,'below changed'),(6,'last changed')]:
        r=apply(root,old,from_line=line,to_line=line,new_text=new)
        assert r['applied'],r
    r=apply(root,p,new_text='X\n')
    assert r['applied'],r
    assert (root/'f.txt').read_text()=='long\nabove\nleft\nX\nanchor\nright\nbelow changed\nlast changed\n'


def test_replaced_right_neighbor_and_same_point_insertion_refuse(root):
    (root/'f.txt').write_text('left\nright\n')
    old=read(root)
    p=point(root,old,2)
    assert apply(root,old,from_line=2,to_line=2,new_text='new')['applied']
    assert not apply(root,p,new_text='X\n')['applied']
    citations.forget_run(RUN)
    old=read(root)
    p=point(root,old,2)
    assert apply(root,p,new_text='X\n')['applied']
    r=apply(root,p,new_text='Y\n')
    assert not r['applied'] and 'exactly there' in r['error'],r


def test_evicted_file_history_refuses_even_when_text_unchanged(root,monkeypatch):
    monkeypatch.setattr(citations,'MAX_JOURNAL_FILES',2)
    (root/'f.txt').write_text('same\n')
    old=read(root)
    for name in ('g.txt','h.txt'):
        (root/name).write_text('same\n')
        unified_read({'working_tree':[('repo',str(root))],'named':{},'allowed':set()},name,run_id=RUN)
    r=apply(root,old,from_line=1,to_line=1,new_text='changed')
    assert not r['applied'] and 'history' in r['error'] and 'reread' in r['error'],r
    assert (root/'f.txt').read_text()=='same\n'
