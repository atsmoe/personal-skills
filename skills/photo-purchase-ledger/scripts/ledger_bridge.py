"""Private host I/O for the JS connector adapter. stdout is data for the host only.

Do not relay raw `read`/`image` output to a model. The host filters it before return.
Signed URLs arrive on stdin and are never persisted or included in exceptions.
"""
import argparse
import base64
import datetime as dt
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from bounded_xlsx import file_hash, BoundedXlsx
from ledger_io import read_json, write_new_json
from ledger_state import atomic_json, locked_run, check_inputs
from ledger_online import snapshot_identity, verify_partial
from ledger_model import digest


def download(request):
    url=urllib.parse.urlsplit(request['url'])
    if url.scheme!='https' or url.username or url.password or url.hostname != request['allowed_host'] or not url.hostname.endswith('.myqcloud.com'):
        raise ValueError('Export URL does not match the selected HTTPS Tencent export host')
    path=Path(request['path']).resolve()
    if path.exists() or path.suffix.lower()!='.xlsx':
        raise ValueError('Export output must be a new .xlsx')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):
            raise ValueError('Export redirects are not accepted')
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        with urllib.request.build_opener(NoRedirect).open(request['url'],timeout=60) as response,path.open('xb') as stream:
            size=0
            while chunk:=response.read(1024*1024):
                size+=len(chunk)
                if size>256*1024*1024:
                    raise ValueError('Export exceeds 256 MiB')
                stream.write(chunk)
        with BoundedXlsx(path):
            pass
    except Exception:
        raise ValueError('Export download failed; discard this incomplete export and request a fresh one') from None
    proof={key:request[key] for key in ('file_id','task_id')}
    proof.update(schema='purchase-ledger-export/v1',path=str(path),sha256=file_hash(path),completed_at=dt.datetime.now(dt.timezone.utc).isoformat())
    write_new_json(path.with_suffix('.receipt.json'),proof)
    return proof


def main():
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stdin.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('read','checkpoint','readback','image','refresh','download'))
    parser.add_argument('--run-dir',type=Path)
    args=parser.parse_args()
    request=json.load(sys.stdin)
    if args.operation=='download':
        result=download(request)
    else:
        with locked_run(args.run_dir) as (directory,state):
            check_inputs(state)
            plan=read_json(directory/'online-plan.json')
            if digest(plan)!=state['online'].get('operations_hash'):
                raise ValueError('Online operation plan changed after preparation')
            if args.operation=='read':
                result={'plan':plan,'checkpoint':read_json(directory/'online-checkpoint.json')}
            elif args.operation=='checkpoint':
                old=read_json(directory/'online-checkpoint.json')
                if request['plan_hash']!=state['plan_hash'] or request['cursor'] not in (old['cursor'],old['cursor']+1):
                    raise ValueError('Invalid checkpoint transition')
                if request['cursor']==old['cursor'] and old['in_flight'] is not None:
                    raise ValueError('Another writer or an uncertain operation owns the online checkpoint')
                atomic_json(directory/'online-checkpoint.json',request)
                result={'saved':True}
            elif args.operation=='readback':
                index=request['index']
                if not isinstance(index,int) or not 0<=index<len(plan['operations']):
                    raise ValueError('Invalid operation index')
                atomic_json(directory/f'online-readback-{index}.json',request['data'])
                result={'saved':True}
            elif args.operation=='image':
                allowed=[op['image'] for op in plan['operations'] if 'image' in op]
                original={key:value for key,value in request.items() if key!='offset'}
                if original not in allowed or file_hash(request['path'])!=request['sha256']:
                    raise ValueError('Image is outside the prepared plan or has changed')
                content=base64.b64encode(Path(request['path']).read_bytes()).decode('ascii')
                offset=request.get('offset',0)
                if not isinstance(offset,int) or not 0<=offset<len(content):
                    raise ValueError('Invalid image chunk offset')
                result={'content':content[offset:offset+200000],'total_chars':len(content)}
            else:
                proof=read_json(Path(request['export']).with_suffix('.receipt.json'))
                if proof['file_id']!=plan['target']['file_id'] or proof['sha256']!=file_hash(request['export']):
                    raise ValueError('Fresh export belongs to a different target or content')
                age=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(proof['completed_at'])).total_seconds()
                if not 0<=age<=300:
                    raise ValueError('Write preflight requires a fresh export')
                checkpoint=read_json(directory/'online-checkpoint.json')
                cursor=checkpoint['cursor']
                if checkpoint['in_flight'] is not None:
                    operation=plan['operations'][cursor]['tool']
                    if operation in ('sheet_insert_dimension','sheet_set_range_value','sheet_insert_image'):
                        try:
                            verify_partial(state,request['export'],plan['operations'],cursor+1)
                            checkpoint.update(cursor=cursor+1,in_flight=None)
                        except ValueError:
                            verify_partial(state,request['export'],plan['operations'],cursor)
                            checkpoint['in_flight']=None
                    else:
                        # Formatting assignments are idempotent; verify the entire
                        # already-written content, then repeat only this assignment.
                        verify_partial(state,request['export'],plan['operations'],cursor)
                        checkpoint['in_flight']=None
                    atomic_json(directory/'online-checkpoint.json',checkpoint)
                elif cursor:
                    verify_partial(state,request['export'],plan['operations'],cursor)
                elif snapshot_identity(request['export'],state['plan']['config'])!=plan['before_identity']:
                    raise ValueError('Online workbook changed; prepare a fresh plan before any write')
                result={'fresh':True,'checkpoint':checkpoint}
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))


if __name__=='__main__':
    try:
        main()
    except (ValueError,OSError,KeyError,TypeError) as error:
        print(json.dumps({'status':'failed','error':str(error)[:240]},ensure_ascii=False))
        raise SystemExit(1)
