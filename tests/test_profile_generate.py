"""Profile updates use configured Compose paths, existing ownership and fail before writes."""
import ast,json,posixpath,re,secrets,shlex,shutil,tempfile,types,unittest
from pathlib import Path
src=Path(__file__).resolve().parents[1]/'src/s5a3_deploy_tool.py'
tree=ast.parse(src.read_text(encoding='utf-8-sig'))
f=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='do_profile_generate')
ns=dict(q=shlex.quote,json=json,posixpath=posixpath,re=re,secrets=secrets,shutil=shutil,tempfile=tempfile,os=__import__('os'),PVF_REL='runtime/docker/data/Script.pvf',time=types.SimpleNamespace(strftime=lambda _: 'fixture-date',sleep=lambda _:None))
exec(compile(ast.Module(body=[f],type_ignores=[]),'profile-update','exec'),ns)
EXE='a'*64;PVF='b'*64
class SSH:
 def __init__(self,owner_ok=True):self.owner_ok=owner_ok;self.commands=[];self.uploads=[]
 def capture(self,c):
  self.commands.append(c)
  if c.startswith('stat '):return (0,'1500:1500') if self.owner_ok else (1,'')
  if c.startswith('openssl '):return 0,'SHA2-256= '+'c'*64
  if c.startswith('sha256sum '):return 0,PVF
  if '/hub/state.json' in c:return 0,json.dumps({'published':{'sequence':1}})
  return 0,json.dumps({'UpdatePublicKey':'fixture'})
 def run(self,c,*args,**kwargs):self.commands.append(c);return 0
 def sftp_put(self,local,remote):self.uploads.append((remote,json.loads(Path(local).read_text())))
 def close(self):pass
class Tool:
 do_profile_generate=ns['do_profile_generate']
 def __init__(self,cli):self.cli=cli
 def _log(self,*args):pass
 def _line_cb(self,*args):pass
 def _local_client_sha(self,cfg):return {'DNF.exe':EXE,'Script.pvf':PVF}
 def _ssh(self,cfg):return self.cli
 def _pm_compose(self,cfg):return 'docker compose --env-file /custom/data/compose.env'
 def _bootstrap(self,cli):return {'sequence':2,'profile':{'ExeSha256':EXE,'PvfSha256':PVF}}
 def _verify_profile_signature(self,*args):return True
 def _export_launcher_profile(self,*args):pass
 def _publish_generated_launcher(self,*args):pass
class ProfileTests(unittest.TestCase):
 cfg={'host':'192.0.2.1','path':'/game','pm_data':'/custom/data'}
 def test_compose_update_backs_up_and_preserves_numeric_owner(self):
  c=SSH();Tool(c).do_profile_generate(self.cfg)
  script='\n'.join(c.commands)
  self.assertIn("stat -c '%u:%g' /custom/data/private/player-profile.json",script)
  self.assertIn('install -o 1500 -g 1500 -m 600',script)
  self.assertIn('cp -p /custom/data/private/player-profile.json',script)
  self.assertIn('docker compose --env-file /custom/data/compose.env restart gateway',script)
  self.assertNotIn('systemctl',script);self.assertNotIn('dnf90gm',script)
  self.assertEqual(c.uploads[1][1]['published']['sequence'],2)
 def test_unknown_owner_prevents_any_profile_upload(self):
  c=SSH(False)
  with self.assertRaisesRegex(RuntimeError,'属主'):Tool(c).do_profile_generate(self.cfg)
  self.assertEqual(c.uploads,[])
if __name__=='__main__':unittest.main()
