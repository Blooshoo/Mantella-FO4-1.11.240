import pathlib,os,sys,re,json,subprocess,concurrent.futures,shutil
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'2026-11-04'))
import compile_plugin as b
header=b.R/'native/port240/include/mantella240/offsets240.h';original=header.read_text()
cases=[('TESTopicInfo::SetOverrideFileName','main.cpp'),('BSAudioManager::ClearCache','main.cpp'),('ViewCasterUpdate event source','Crosshair.cpp'),('BSPointerHandle lookup (crosshair)','Crosshair.cpp'),('TakeScreenshot','main.cpp')]
def run(case):
 name,src=case;key=re.sub('[^a-zA-Z0-9]','_',name);root=b.R/'negative'/key;shutil.copytree(b.R/'native/port240/include/mantella240',root/'mantella240',dirs_exist_ok=True)
 marker='OffsetSpec{ "'+name+'"'
 matches=[line for line in original.splitlines() if marker in line]
 assert len(matches)==1, (name, matches)
 old=matches[0]
 assert old.count('Status::Unresolved')+old.count('Status::LocatedAbiUnproven')==1
 new=old.replace('Status::Unresolved','Status::Verified').replace('Status::LocatedAbiUnproven','Status::Verified')
 s=original.replace(old,new)
 (root/'mantella240/offsets240.h').write_text(s)
 shutil.copy(b.R/'native/port240/plugin/Guard240.h',root/'Guard240.h')
 cmd=[str(b.T/'clang-cl'),'/I'+str(root),*b.flags,'/W0','-fsyntax-only','/FI'+str(b.R/'native'/'upstream'/'TopicInfoPatcher'/'src'/'PCH.h'),str(b.R/'native/upstream/TopicInfoPatcher/src'/src)]
 p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=b.env,timeout=150,text=True)
 (b.R/'evidence'/('tripwire-'+key+'.log')).write_text(p.stdout)
 return dict(name=name,exit=p.returncode,static_assert='static assertion failed' in p.stdout,command=cmd)
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(run,cases))
root=b.R/'negative/TakeScreenshot'
cmd=[str(b.T/'clang-cl'),'/I'+str(root),*b.flags,'/W0','-fsyntax-only',str(b.R/'native/port240/tests/test_guard.cpp')]
p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=b.env,timeout=150,text=True)
(b.R/'evidence/tripwire-screenshot-test-guard.log').write_text(p.stdout)
results.append(dict(name='TakeScreenshot (test_guard.cpp)',exit=p.returncode,static_assert='static assertion failed' in p.stdout,command=cmd))
(b.R/'evidence/tripwire-results.json').write_text(json.dumps(results,indent=2));print(json.dumps([{k:v for k,v in r.items() if k!='command'} for r in results],indent=2))

# Expected contract of frozen ffffcad only; inspect/update after a reviewed source repair.
assert all(x['exit'] != 0 and x['static_assert'] for x in results[:4])
assert results[4]['exit'] == 0
assert results[5]['exit'] != 0 and results[5]['static_assert']
