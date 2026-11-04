import compile_plugin as b,subprocess,json
name='dep-deps_commonlibf4_src_F4SE_API'
if b.compile_one('deps/commonlibf4/src/F4SE/API.cpp',name,['/W0','/FI'+str(b.R/'generated/api_macro_compat.h')]):raise SystemExit(1)
objects=sorted(b.OBJ.glob('dep-*.obj'))
# Unused RegistrationMap/Unique units have upstream bare-return compile errors.
# Neither is needed by HTTP; final link must resolve everything without /force.
cmd=[str(b.T/'llvm-lib'),'/nologo','/out:'+str(b.OBJ/'commonlib-http-subset.lib'),*map(str,objects)]
(b.R/'evidence/library-subset-command.json').write_text(json.dumps(cmd,indent=2));subprocess.run(cmd,check=True,cwd=b.R,env=b.env)
libs=['kernel32','winhttp','advapi32','bcrypt','d3d11','d3dcompiler','dbghelp','dxgi','ole32','shell32','user32','version','ws2_32']
cmd=[str(b.T/'lld-link'),'/nologo','/dll','/machine:x64','/opt:ref','/opt:icf','/incremental:no','/timestamp:0','/out:'+str(b.R/'staging/F4SE_HTTP.dll'),'/libpath:'+str(b.R/'sdk/crt/lib/x86_64'),'/libpath:'+str(b.R/'sdk/sdk/lib/ucrt/x86_64'),'/libpath:'+str(b.R/'sdk/sdk/lib/um/x86_64'),str(b.OBJ/'http.obj'),str(b.OBJ/'transport.obj'),str(b.OBJ/'commonlib-http-subset.lib'),*[x+'.lib' for x in libs]]
(b.R/'evidence/link-command.json').write_text(json.dumps(cmd,indent=2))
with (b.R/'evidence/link-http.log').open('w') as f:r=subprocess.run(cmd,cwd=b.R,env=b.env,stdout=f,stderr=subprocess.STDOUT)
print('LINK EXIT',r.returncode);print((b.R/'evidence/link-http.log').read_text());raise SystemExit(r.returncode)
