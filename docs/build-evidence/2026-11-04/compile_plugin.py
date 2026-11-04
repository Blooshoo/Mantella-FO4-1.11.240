import pathlib,subprocess,json,sys,os
R=pathlib.Path(os.environ['MANTELLA_BUILD_ROOT']).resolve()
T=pathlib.Path(os.environ['LLVM_BIN']).resolve()
OBJ=R/'obj';OBJ.mkdir(exist_ok=True)
flags=['/nologo','--target=x86_64-pc-windows-msvc','/std:c++latest','/EHsc','/MD','/O2','/DNDEBUG','/D_CRT_USE_BUILTIN_OFFSETOF','/DUNICODE','/D_UNICODE','/DNOMINMAX','/DWIN32_LEAN_AND_MEAN','/DSPDLOG_USE_STD_FORMAT','/DSPDLOG_WCHAR_TO_UTF8_SUPPORT','-fms-compatibility-version=19.44']
incs=['sdk/crt/include','sdk/sdk/include/ucrt','sdk/sdk/include/um','sdk/sdk/include/shared','sdk/sdk/include/winrt','deps/commonlibf4/include','deps/commonlibf4/lib/commonlib-shared/include','deps/spdlog/include','native/port240/http/include','native/port240/include','native/port240/plugin','generated']
flags += ['/I'+str(R/p) for p in incs]
env=os.environ.copy()
for k in ['XSE_FO4_GAME_PATH','XSE_FO4_MODS_PATH','Fallout4Path','MO2_MODS_FOLDER','VCPKG_INSTALLATION_ROOT']:env.pop(k,None)
def compile_one(src,name,extra=[]):
 cmd=[str(T/'clang-cl'),*flags,*extra,'/c',str(R/src),'/Fo'+str(OBJ/(name+'.obj'))]
 (R/'evidence'/('command-'+name+'.json')).write_text(json.dumps(cmd,indent=2))
 with (R/'evidence'/(name+'.log')).open('w') as f:
  p=subprocess.run(cmd,cwd=R,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=150)
 print(name,'exit',p.returncode,flush=True)
 print('\n'.join((R/'evidence'/(name+'.log')).read_text().splitlines()[-28:]),flush=True)
 return p.returncode
if __name__=='__main__':
 if sys.argv[1]=='http':sys.exit(compile_one('native/port240/http/plugin/F4SE_HTTP240.cpp','http'))
 if sys.argv[1]=='transport':sys.exit(compile_one('native/port240/http/plugin/transport_winhttp.cpp','transport'))
 if sys.argv[1]=='topic':
  g=R/'generated';g.mkdir(exist_ok=True)
  s=(R/'native/upstream/TopicInfoPatcher/cmake/Version.h.in').read_text()
  for a,b in {'@PROJECT_VERSION_MAJOR@':'1','@PROJECT_VERSION_MINOR@':'0','@PROJECT_VERSION_PATCH@':'0','@PROJECT_VERSION@':'1.0.0','@PROJECT_NAME@':'TopicInfoPatcher'}.items():s=s.replace(a,b)
  (g/'Version.h').write_text(s)
  sys.exit(compile_one('native/upstream/TopicInfoPatcher/src/main.cpp','topic',['/FI'+str(R/'native/upstream/TopicInfoPatcher/src/PCH.h')]))
