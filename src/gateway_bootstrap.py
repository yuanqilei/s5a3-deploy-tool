"""Idempotent first-install gateway preparation, executed only on the SSH host.
No credentials are returned; existing deployment keys are never regenerated.
"""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import ssl
import subprocess
import sys


def run(args, **kwargs):
    p = subprocess.run(args, capture_output=True, **kwargs)
    if p.returncode:
        # Subprocess arguments and SQL may contain credentials; do not echo them.
        raise RuntimeError('Gateway preparation command failed: ' + Path(args[0]).name)
    return p.stdout


def prepare(c):
    src, data, game = (Path(c[k]) for k in ('pm_src', 'pm_data', 'path'))
    for p in (src, data, game):
        if not p.is_absolute() or p == Path('/') or '..' in p.parts:
            raise ValueError('Use absolute deployment directory paths')
    if (data / 'compose.env').is_file():
        for name in ('gateway.json', 'private/player-profile.json', 'private/signing-key.pem'):
            if not (data / name).is_file():
                raise ValueError('Existing gateway is incomplete; restore its backup before continuing')
        print('Existing gateway configuration retained; signing key unchanged.')
        return
    if data.exists():
        raise ValueError('Gateway data directory already exists without compose.env; preserve it and choose a new data directory')
    ip = str(ipaddress.IPv4Address(c['advertise_ip'] or c['host']))
    inst = json.loads((game / 'runtime/docker/instance.json').read_text())
    pvf = game / 'runtime/docker/data/Script.pvf'
    if hashlib.sha256(pvf.read_bytes()).hexdigest() != c['pvf_sha256']:
        raise ValueError('Selected client PVF differs from the running server; no gateway was prepared')
    if len(c['exe_sha256']) != 64:
        raise ValueError('Select the matching local DNF.exe before first installation')
    if not inst.get('player_proxy_secret') or not inst.get('admin_token'):
        raise ValueError('Game runtime lacks gateway secrets; update the game deployment first')
    try:
        user = pwd.getpwnam('pmagic')
    except KeyError:
        run(['useradd', '-r', '-U', '-M', '-s', '/usr/sbin/nologin', 'pmagic'])
        user = pwd.getpwnam('pmagic')
    data.parent.mkdir(parents=True, exist_ok=True)
    inputs = data.parent / (data.name + '-inputs')
    inputs.mkdir(mode=0o700, exist_ok=True)
    signing = inputs / 'signing-key.pem'
    if not signing.exists():
        run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', str(signing)])
        signing.chmod(0o600)
    pvf_input = inputs/'Script.pvf'
    shutil.copyfile(pvf, pvf_input)
    public = run(['openssl', 'pkey', '-in', str(signing), '-pubout']).decode().strip()
    cert, key = inputs/'server.crt', inputs/'server.key'
    if not cert.exists() and not key.exists():
        run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '825', '-subj', '/CN='+ip, '-addext', 'subjectAltName=IP:'+ip, '-addext', 'basicConstraints=critical,CA:TRUE'])
        key.chmod(0o600)
    if not cert.exists() or not key.exists():
        raise ValueError('Partial certificate inputs preserved; restore or complete them first')
    profile = dict(ApiUrl='https://'+ip+':8443', SettingsUrl='https://'+ip+':8443/launcher/settings.json', GameHost=ip, BootstrapPort=7001, ChannelServerId=0, MinimumLauncherVersion='1.0.18', DefaultClientDirectory='', CertificateSha256=hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest(), UpdatePublicKey=public, ExeSha256=c['exe_sha256'], PvfSha256=c['pvf_sha256'])
    approved = inputs/'profile.json'
    approved.write_text(json.dumps(profile, ensure_ascii=False, indent=2))
    # Keep a retry-stable dedicated GM credential; game and root credentials stay unchanged.
    gm_file = inputs/'gm-db.json'
    if gm_file.exists():
        gm = json.loads(gm_file.read_text())
    else:
        gm = {'user': 'pm_' + hashlib.sha256(str(data).encode()).hexdigest()[:8], 'password': secrets.token_hex(24)}
        gm_file.write_text(json.dumps(gm)); gm_file.chmod(0o600)
    container = json.loads(run(['docker', 'inspect', 'servers5a3-db-1']))[0]
    dbhost = next(v['IPAddress'] for v in container['NetworkSettings']['Networks'].values() if v.get('IPAddress'))
    password = gm['password']
    if not all(x in '0123456789abcdef' for x in password):
        raise ValueError('Unexpected dedicated GM credential encoding')
    dbuser = gm['user']
    if not dbuser.startswith('pm_') or not dbuser.replace('_','').isalnum():
        raise ValueError('Unexpected dedicated GM username')
    sql = "CREATE USER IF NOT EXISTS '"+dbuser+"'@'%' IDENTIFIED BY '"+password+"'; ALTER USER '"+dbuser+"'@'%' IDENTIFIED BY '"+password+"'; GRANT ALL PRIVILEGES ON dnf_local.* TO '"+dbuser+"'@'%'; FLUSH PRIVILEGES;"
    run(['docker', 'exec', '-i', '-e', 'MYSQL_PWD='+inst['root_password'], 'servers5a3-db-1', 'mariadb', '-uroot'], input=sql.encode())
    dsn = dbuser+':'+password+'@tcp('+dbhost+':3306)/dnf_local?parseTime=true&charset=utf8mb4,utf8&interpolateParams=true'
    settings = dict(gmOrigin='https://'+ip, approvedProfile=str(approved), signingPrivateKey=str(signing), pvfFile=str(pvf_input), playerCertificate=str(cert), playerPrivateKey=str(key), playerCA=str(cert), webCertificate=str(cert), webPrivateKey=str(key), database=dict(registrationDsn=dsn, repositoryDsn=dsn, accountTable='dnf90_web_accounts', databases=['dnf_local'], tablePrefix='dnf'), game=dict(upstream='http://127.0.0.1:17002', proxySecret=inst['player_proxy_secret'], adminUrl='http://127.0.0.1:17002/api/game/admin', adminToken=inst['admin_token']))
    settings_file=inputs/'settings.json'; settings_file.write_text(json.dumps(settings)); settings_file.chmod(0o600)
    for p in [data.parent, inputs, *inputs.iterdir()]:
        os.chown(p, user.pw_uid, user.pw_gid)
    # Runtime PVF is read-only input; do not chmod or replace it.
    run(['runuser', '-u', 'pmagic', '--', 'python3', str(src/'deploy/selfhost/prepare.py'), '--settings', str(settings_file), '--output', str(data)])
    print('Gateway inputs prepared. Private keys and credentials retained on server.')


if __name__ == '__main__':
    try:
        prepare(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig')))
    except Exception as error:
        raise SystemExit(type(error).__name__+': '+str(error))
