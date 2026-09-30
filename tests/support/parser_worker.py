"""Resource-limit fixture, invoked only by tests with synthetic requests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from materials.extractors.isolation import apply_resource_limits
root=Path(sys.argv[1]); request=json.loads((root/'request.json').read_text())
job=apply_resource_limits(request['limits'])
operation=request['operation']
if operation=='memory':
    try:
        allocation=bytearray(512*1024**2)
        result={'limited':False}
    except MemoryError:
        result={'limited':True}
elif operation=='sleep':
    child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(60)'],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    Path(request['pid_path']).write_text(str(child.pid))
    time.sleep(60); result={}
elif operation=='disk':
    (root/'overflow').write_bytes(b'x'*(10*1024**2)); time.sleep(5); result={}
else:
    result={'secret_present': 'ARTI_TEST_SECRET' in os.environ,
            'provider_imported': any(k in sys.modules for k in ('config','ai.generation'))}
(root/'result.json').write_text(json.dumps(result))
