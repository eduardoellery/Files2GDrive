#!/usr/bin/python3
"""Validate an extracted package using system Python and a throwaway profile.

No Google login is performed, no browser/tray is opened and no existing profile
is read. The new worker requests its empty keyring namespace only.
"""
import argparse
from contextlib import redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
import time


def validate(root):
    wrapper=root/'usr/bin/files2gdrive'
    with TemporaryDirectory(prefix='files2gdrive-installed-profile-') as temporary:
        state=Path(temporary)/'state'
        sys.argv=[str(wrapper),'--state-dir',str(state),'--help']
        with redirect_stdout(io.StringIO()):
            try: runpy.run_path(str(wrapper),run_name='__main__')
            except SystemExit as error:
                if error.code not in (None,0): raise
        from files2gdrive import packaged_launcher,runtime_context
        assert Path(packaged_launcher.__file__).is_relative_to(root/'usr/lib/files2gdrive/app')
        assert runtime_context.command(state,'serve')[0]==str(wrapper)
        packaged_launcher.prepare_new_state(state,root/'usr/share/files2gdrive')
        from files2gdrive.database import Database
        db=Database(state/'state.sqlite3')
        try:
            assert db.get('backend')=='drive'
            assert db.connection.execute('SELECT count(*) FROM roots').fetchone()[0]==0
        finally: db.close()
        from googleapiclient.discovery import build
        from google.auth.credentials import AnonymousCredentials
        service=build('drive','v3',credentials=AnonymousCredentials(),cache_discovery=False,static_discovery=True)
        assert hasattr(service,'files')
        from files2gdrive.desktop import ensure_engine
        from files2gdrive.desktop_ipc import request,LocalControlError
        started=False
        try:
            ensure_engine(state,timeout=15); started=True
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                status=request(state,'status')
                if status['auth_state']=='required': break
                time.sleep(.1)
            assert status['auth_state']=='required',status['auth_state']
            assert status['active_roots']==0
            request(state,'pause'); assert request(state,'status')['paused']
            request(state,'resume'); assert not request(state,'status')['paused']
        finally:
            if started:
                try: request(state,'quit')
                except LocalControlError: pass
                deadline=time.monotonic()+15
                with (state/'engine.lock').open('a') as lock:
                    while time.monotonic()<deadline:
                        try:
                            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                            break
                        except BlockingIOError: time.sleep(.1)
                    else: raise AssertionError('Packaged worker did not shut down')
        report=dict(success=True,system_python=sys.executable,wrapper_reexec=True,
                    new_profile_backend='drive',new_profile_auth='required',active_roots=0,
                    google_login=False,google_upload=False,controls=['pause','resume','quit'])
        print(json.dumps(report,indent=2))
        return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args()
    validate(args.root.resolve())
