import compile_plugin as b
import concurrent.futures,subprocess,json
sources=[]
for root,pch in [('deps/commonlibf4/lib/commonlib-shared','deps/commonlibf4/lib/commonlib-shared/src/REX/PCH.h'),('deps/commonlibf4','deps/commonlibf4/include/F4SE/Impl/PCH.h')]:
 for p in sorted((b.R/root/'src').rglob('*.cpp')):
  rel=p.relative_to(b.R);name='dep-'+str(rel).replace('/','_').replace('.cpp','')
  sources.append((str(rel),name,['/W0','/FI'+str(b.R/pch)]))
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
 results=list(ex.map(lambda args:(args[1],b.compile_one(*args)),sources))
(b.R/'evidence/dependency-results.json').write_text(json.dumps(results,indent=2))
failed=[x for x in results if x[1]]
print('COMPILED',len(results),'FAILED',len(failed),failed,flush=True)
if failed:raise SystemExit(1)
cmd=[str(b.T/'llvm-lib'),'/nologo','/out:'+str(b.OBJ/'commonlib-combined.lib'),*[str(b.OBJ/(x[1]+'.obj')) for x in sources]]
(b.R/'evidence/library-command.json').write_text(json.dumps(cmd,indent=2))
raise SystemExit(subprocess.call(cmd,cwd=b.R,env=b.env))
