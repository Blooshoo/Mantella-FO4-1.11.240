import os
import struct,json,hashlib,pathlib
R=pathlib.Path(os.environ['MANTELLA_BUILD_ROOT']).resolve();p=R/'staging/F4SE_HTTP.dll';d=p.read_bytes()
def u16(o):return struct.unpack_from('<H',d,o)[0]
def u32(o):return struct.unpack_from('<I',d,o)[0]
def s(o):return d[o:d.index(b'\0',o)].decode('ascii')
assert d[:2]==b'MZ';pe=u32(0x3c);assert d[pe:pe+4]==b'PE\0\0'
coff=pe+4;opt=coff+20;assert u16(coff)==0x8664 and u16(opt)==0x20b and u16(coff+18)&0x2000
sec=opt+u16(coff+16);sections=[]
for i in range(u16(coff+2)):
 o=sec+40*i;sections.append(dict(name=d[o:o+8].rstrip(b'\0').decode(),vsize=u32(o+8),rva=u32(o+12),rawsize=u32(o+16),raw=u32(o+20),flags=u32(o+36)))
def off(r):
 for x in sections:
  if x['rva']<=r<x['rva']+max(x['vsize'],x['rawsize']):return x['raw']+r-x['rva']
 raise ValueError(hex(r))
e=off(u32(opt+112));exports={}
for i in range(u32(e+24)):
 name=s(off(u32(off(u32(e+32))+4*i)));ordinal=u16(off(u32(e+36))+2*i);exports[name]=u32(off(u32(e+28))+4*ordinal)
assert set(exports)=={'F4SEPlugin_Load','F4SEPlugin_Version'}
v=off(exports['F4SEPlugin_Version']);metadata=dict(data_version=u32(v),plugin_version=hex(u32(v+4)),name=s(v+8),author=s(v+0x108),address_independence=u32(v+0x208),structure_independence=u32(v+0x20c),compatible_versions=[hex(x) for x in struct.unpack_from('<16I',d,v+0x210)],minimum_f4se=hex(u32(v+0x250)))
assert metadata['data_version']==1 and metadata['name']=='F4SE_HTTP'
assert metadata['address_independence']==0 and metadata['structure_independence']==0
assert struct.unpack_from('<16I',d,v+0x210)==(0x010b0f00,)+(0,)*15
assert u32(v+4)==0x01f00000
imports=[];imp=off(u32(opt+120))
while any(d[imp:imp+20]):imports.append(s(off(u32(imp+12))));imp+=20
assert 'WINHTTP.dll'.lower() in [x.lower() for x in imports]
assert not any('cpr' in x.lower() or 'curl' in x.lower() or 'spdlog' in x.lower() or 'commonlib' in x.lower() for x in imports)
load=exports['F4SEPlugin_Load'];assert any(x['rva']<=load<x['rva']+x['vsize'] and x['flags']&0x20000000 for x in sections)
result=dict(file=str(p),bytes=len(d),sha256=hashlib.sha256(d).hexdigest(),machine='AMD64',format='PE32+ DLL',entry_rva=hex(u32(opt+16)),exports={k:hex(x) for k,x in exports.items()},metadata=metadata,imports=imports,sections=sections,verification='PASS: binary structure, exports and exact-runtime declaration only; not a runtime test')
(R/'evidence/pe-verification.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
