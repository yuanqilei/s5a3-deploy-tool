import ast, json, posixpath, re, shlex, tempfile, unittest, os, shutil, secrets, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
tree=ast.parse((ROOT/'src/s5a3_deploy_tool.py').read_text(encoding='utf-8-sig'))
names={'_detect_release_dir','do_launcher_upload','_gm_profile','_export_launcher_profile'}
ns=dict(posixpath=posixpath,re=re,q=shlex.quote,GM_BASE='/opt/purplemagic',DEFAULTS={'pm_data':'/opt/purplemagic/data2'},json=json,os=os,shutil=shutil,time=time,human_size=str)
exec(compile(ast.Module(body=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[]),'gateway-methods','exec'),ns)
class Tool:
    _detect_release_dir=ns['_detect_release_dir']; do_launcher_upload=ns['do_launcher_upload']; _gm_profile=ns['_gm_profile']; _export_launcher_profile=ns['_export_launcher_profile']
    def __init__(self,text='',code='200',rc=0): self.text=text; self.code=code; self.rc=rc; self.logs=[]; self.uploaded=[]
    def _remote_text(self,cli,path): return self.text if path.startswith('/custom/data/') else ''
    def _log(self,*args): self.logs.append(args[0])
    def _ssh(self,cfg): return self
    def _mkdir(self,*args): pass
    def _put_verified(self,cli,local,dst,label): self.uploaded.append(dst)
    def capture(self,cmd): self.command=cmd; return self.rc,self.code
    def close(self): pass
class GatewayTests(unittest.TestCase):
    def test_container_release_maps_to_selected_host_mount(self):
        tool=Tool('{"release_dir":"/data/releases"}')
        self.assertEqual(tool._detect_release_dir(tool,{'pm_data':'/custom/data'}),'/custom/data/releases')
    def test_profile_uses_selected_data_dir(self):
        tool=Tool('{"PvfSha256":"fixture"}')
        self.assertEqual(tool._gm_profile(tool,{'pm_data':'/custom/data'})[1]['PvfSha256'],'fixture')
    def test_missing_gateway_never_uploads_or_reports_success(self):
        with tempfile.NamedTemporaryFile() as f:
            tool=Tool()
            with self.assertRaisesRegex(RuntimeError,'网关尚未初始化'): tool.do_launcher_upload({'launcher_exe':f.name,'pm_data':'/custom/data','host':'192.0.2.1'})
            self.assertEqual(tool.uploaded,[])
            self.assertFalse(any('上传完成' in x for x in tool.logs))
    def test_failed_download_never_reports_success(self):
        with tempfile.NamedTemporaryFile() as f:
            tool=Tool('{"release_dir":"/data/releases"}',code='000',rc=7)
            with self.assertRaisesRegex(RuntimeError,'发布未完成'): tool.do_launcher_upload({'launcher_exe':f.name,'pm_data':'/custom/data','host':'192.0.2.1'})
            self.assertFalse(any('上传完成' in x for x in tool.logs)); self.assertIn('https://127.0.0.1:8443',tool.command)
    def test_success_uses_https_and_selected_mount(self):
        with tempfile.NamedTemporaryFile() as f:
            tool=Tool('{"release_dir":"/data/releases"}')
            tool.do_launcher_upload({'launcher_exe':f.name,'pm_data':'/custom/data','host':'192.0.2.1'})
            self.assertEqual(tool.uploaded,['/custom/data/releases/PurpleMagic.exe'])
            self.assertTrue(any('上传完成' in x for x in tool.logs))
    def test_profile_export_backs_up_previous_operator_files(self):
        with tempfile.TemporaryDirectory() as d:
            ns['APP_DIR']=ns['RES_DIR']=d
            Path(d,'launcher-template.exe').write_bytes(b'fixture')
            tool=Tool('{"UpdatePublicKey":"PUBLIC FIXTURE","ApiUrl":"https://192.0.2.1:8443"}')
            tool._export_launcher_profile(tool,{'pm_data':'/custom/data'})
            tool._export_launcher_profile(tool,{'pm_data':'/custom/data'})
            target=Path(d,'玩家启动器'); self.assertEqual((target/'PurpleMagic.exe').read_bytes(),b'fixture')
            self.assertTrue(list(target.glob('PurpleMagic.profile.json.bak-*')))
if __name__=='__main__': unittest.main()
