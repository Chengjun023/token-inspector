"""Reference artifacts for offline plumbing demonstrations ONLY; never model fixtures."""
import json

SOURCES = {
"routine_code": '''def union_ranges(ranges):
    if not isinstance(ranges, list): raise ValueError()
    clean=[]
    for r in ranges:
        if not isinstance(r,(list,tuple)) or len(r)!=2: raise ValueError()
        a,b=r
        if type(a) is not int or type(b) is not int or a>b: raise ValueError()
        if a<b: clean.append([a,b])
    out=[]
    for a,b in sorted(clean):
        if out and a<=out[-1][1]: out[-1][1]=max(out[-1][1],b)
        else: out.append([a,b])
    return out
''',
"bug_fix": '''from collections import OrderedDict
class Cache:
    def __init__(self,capacity):
        if type(capacity) is not int or capacity<=0: raise ValueError()
        self.capacity=capacity
        self.data=OrderedDict()
    def purge(self,now):
        for key in list(self.data):
            if self.data[key][1]<=now: del self.data[key]
    def put(self,key,value,now,ttl):
        self.purge(now)
        self.data.pop(key,None)
        if ttl==0: return
        if len(self.data)>=self.capacity: self.data.popitem(last=False)
        self.data[key]=(value,now+ttl)
    def get(self,key,now):
        self.purge(now)
        value,expiry=self.data[key]
        self.data.move_to_end(key)
        return value
''',
"weighted_schedule": '''def select_jobs(jobs):
    if not isinstance(jobs,list): raise ValueError()
    seen=set()
    for j in jobs:
        if not isinstance(j,dict) or set(j)!={'id','start','end','reward'}: raise ValueError()
        if not isinstance(j['id'],str) or not j['id'] or j['id'] in seen: raise ValueError()
        if any(type(j[k]) is not int for k in ('start','end','reward')) or j['start']>=j['end']: raise ValueError()
        seen.add(j['id'])
    ordered=sorted(jobs,key=lambda j:(j['end'],j['start'],j['id']))
    states=[(0,())]
    def rank(s): return (-s[0],len(s[1]),s[1])
    for i,j in enumerate(ordered):
        p=i-1
        while p>=0 and ordered[p]['end']>j['start']: p-=1
        base=states[p+1]
        take=(base[0]+j['reward'],base[1]+(j['id'],))
        states.append(min((states[-1],take),key=rank))
    return list(states[-1][1])
''',
"circular_boundaries": '''def owner(period,segments,t):
    if type(period) is not int or period<=0 or type(t) is not int or not isinstance(segments,list): raise ValueError()
    seen=set(); candidates=[]
    for s in segments:
        if not isinstance(s,dict) or set(s)!={'id','start','end','priority'}: raise ValueError()
        if not isinstance(s['id'],str) or not s['id'] or s['id'] in seen: raise ValueError()
        if any(type(s[k]) is not int for k in ('start','end','priority')): raise ValueError()
        if not 0<=s['start']<period or not 0<=s['end']<period: raise ValueError()
        seen.add(s['id'])
        length=(s['end']-s['start'])%period or period
        if (t-s['start'])%period<length: candidates.append((-s['priority'],length,s['id']))
    return min(candidates)[2] if candidates else None
'''}


def answer(task_id):
    try:
        from .tasks import FIXTURES
    except ImportError:
        from tasks import FIXTURES
    if task_id == "exact_edit":
        path, content = "config.ini", (FIXTURES / "config.ini").read_text().replace("retry_count = 3  #", "retry_count = 5  #", 1)
    elif task_id == "structured_extract":
        path = "summary.json"
        content = json.dumps({"invoices": [{"id": "A", "customer": "Maple", "amount": "10.10"},
                                           {"id": "E", "customer": "Pine", "amount": "1.20"},
                                           {"id": "F", "customer": "Maple", "amount": "1.05"}],
                              "totals": [{"customer": "Maple", "amount": "11.15"},
                                         {"customer": "Pine", "amount": "1.20"}]})
    else:
        path, content = ("cache.py" if task_id == "bug_fix" else "solution.py"), SOURCES[task_id]
    return {"files": [{"path": path, "content": content}]}
