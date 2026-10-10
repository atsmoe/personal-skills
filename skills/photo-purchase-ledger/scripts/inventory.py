"""Inventory images in Windows paste order, without changing source files."""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')

def inventory(day):
    images=[]
    for path in day.iterdir():
        if not path.is_file() or path.suffix.lower() not in {'.jpg','.jpeg','.png','.webp','.bmp','.tif','.tiff'}:
            continue
        stat=path.stat()
        created=getattr(stat,'st_birthtime',stat.st_ctime if os.name=='nt' else None)
        if created is None:
            raise RuntimeError('Creation time unavailable; obtain original Windows CreationTime instead of sorting hash names.')
        images.append({'file':path.name,'creation_timestamp':created,'creation_time':dt.datetime.fromtimestamp(created).isoformat(),'bytes':stat.st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    images.sort(key=lambda x:x['creation_timestamp'])
    times={}
    hashes={}
    for i,item in enumerate(images,1):
        item['index']=i
        times.setdefault(item['creation_timestamp'],[]).append(item['file'])
        hashes.setdefault(item['sha256'],[]).append(item['file'])
    return {'folder':str(day),'images':images,'time_ties':[v for v in times.values() if len(v)>1],'duplicate_bytes':[v for v in hashes.values() if len(v)>1]}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day',required=True,type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args();day=args.day.resolve()
    if not day.is_dir():parser.error('Date folder does not exist')
    result=inventory(day)
    output=args.output or day/('.work_'+day.name)/'inventory.json'
    output=output.resolve()
    if day not in output.parents:parser.error('Auxiliary output must stay inside the date folder')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(output),'time_ties':result['time_ties'],'duplicate_bytes':result['duplicate_bytes']},ensure_ascii=False))

if __name__=='__main__':main()
