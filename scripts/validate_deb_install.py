#!/usr/bin/python3
"""Install/reinstall/purge in a disposable dpkg root, never the host database.

Run as root because Debian payload owners are root:root. The package is rejected
if it has maintainer scripts. Dependency checks are intentionally forced in the
empty database; dependencies are separately checked by apt simulation/runtime.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def validate(artifact, report_path=None):
    if os.geteuid()!=0:
        raise RuntimeError('Use sudo for isolated dpkg ownership checks')
    artifact=artifact.resolve()
    result={}
    with tempfile.TemporaryDirectory(prefix='files2gdrive-dpkg-install-') as temporary:
        base=Path(temporary); root=base/'root'; root.mkdir()
        control=base/'control'
        subprocess.run(['dpkg-deb','--control',str(artifact),str(control)],check=True,capture_output=True)
        assert not any((control/name).exists() for name in ('preinst','postinst','prerm','postrm','config','triggers'))
        admin=root/'var/lib/dpkg'; admin.mkdir(parents=True); (admin/'status').touch()
        state=root/'home/test-user/.local/state/files2gdrive'
        state.mkdir(parents=True)
        sentinel=state/'state-sentinel'; sentinel.write_bytes(b'Existing user profile must survive package removal')
        before=hashlib.sha256(sentinel.read_bytes()).hexdigest()
        prefix=['dpkg','--root='+str(root),'--force-depends']
        subprocess.run([*prefix,'--install',str(artifact)],check=True,capture_output=True,text=True)
        assert (root/'usr/bin/files2gdrive').is_file()
        query=subprocess.check_output(['dpkg-query','--admindir='+str(admin),'-W','-f=${Status}','files2gdrive'],text=True)
        assert query=='install ok installed',query
        subprocess.run([*prefix,'--install',str(artifact)],check=True,capture_output=True,text=True)
        assert hashlib.sha256(sentinel.read_bytes()).hexdigest()==before
        subprocess.run([*prefix,'--remove','files2gdrive'],check=True,capture_output=True,text=True)
        assert not (root/'usr/bin/files2gdrive').exists()
        assert sentinel.is_file()
        subprocess.run([*prefix,'--purge','files2gdrive'],check=True,capture_output=True,text=True)
        assert not (root/'etc/xdg/autostart/files2gdrive.desktop').exists()
        assert hashlib.sha256(sentinel.read_bytes()).hexdigest()==before
        result=dict(success=True,isolated_dpkg_root=True,host_installation_changed=False,
                    install=True,reinstall=True,remove=True,purge=True,user_profile_preserved=True,
                    maintainer_scripts=False,dependency_checks='forced in empty isolated database')
    if report_path:
        report_path.write_text(json.dumps(result,indent=2)+'\n')
        report_path.chmod(0o644)
        if 'SUDO_UID' in os.environ:
            os.chown(report_path,int(os.environ['SUDO_UID']),int(os.environ['SUDO_GID']))
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifact',type=Path); parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    validate(args.artifact,args.report)
