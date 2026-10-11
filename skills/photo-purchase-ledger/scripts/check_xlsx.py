"""Read-only OOXML checks; optional expected cached numeric cell values."""
import argparse
import json
import math
import posixpath
from decimal import Decimal
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
from bounded_xlsx import BoundedXlsx
from ledger_io import add_summary_arguments, emit_summary, error_summary, write_new_json
from uuid import uuid4

if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')

NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main','r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}

def check_drawing_geometry(z,member,sh_xml,padding_x=14,padding_y=8,digit_width=7):
    """Use the saved sheet dimensions and merged hosts for both anchor types."""
    errors=[]
    x={'x':'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'}
    fmt=sh_xml.find('s:sheetFormatPr',NS)
    default_height=float(fmt.attrib.get('defaultRowHeight',15)) if fmt is not None else 15
    default_width=float(fmt.attrib.get('defaultColWidth',8.43)) if fmt is not None else 8.43
    heights={int(r.attrib['r']):float(r.attrib.get('ht',default_height))*12700 for r in sh_xml.findall('s:sheetData/s:row',NS)}
    columns=sh_xml.findall('s:cols/s:col',NS)
    def cw(col):
        width=default_width
        for c in columns:
            if int(c.attrib['min'])<=col<=int(c.attrib['max']):width=float(c.attrib.get('width',default_width))
        return math.floor(width*digit_width+5)*9525
    def rh(row):return heights.get(row,default_height*12700)
    def address(ref):
        letters,number=re.fullmatch(r'([A-Z]+)(\d+)',ref).groups();col=0
        for letter in letters:col=col*26+ord(letter)-64
        return int(number),col
    merges=[]
    for m in sh_xml.findall('s:mergeCells/s:mergeCell',NS):
        ends=m.attrib['ref'].split(':');merges.append((*address(ends[0]),*address(ends[-1])))
    relpath=posixpath.join(posixpath.dirname(member),'_rels',posixpath.basename(member)+'.rels')
    if relpath not in z.namelist():return errors
    rels={r.attrib['Id']:r.attrib['Target'] for r in ET.fromstring(z.read(relpath))}
    for node in sh_xml.findall('s:drawing',NS):
        target=rels[node.attrib['{'+NS['r']+'}id']]
        path=target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(member),target))
        for idx,d in enumerate(ET.fromstring(z.read(path)),1):
            fr=d.find('x:from',x)
            if fr is None:
                errors.append(f'Drawing {idx}: absolute anchor has no verifiable cell host');continue
            row=int(fr.find('x:row',x).text)+1;col=int(fr.find('x:col',x).text)+1
            ox=int(fr.find('x:colOff',x).text);oy=int(fr.find('x:rowOff',x).text)
            host=next((m for m in merges if m[0]<=row<=m[2] and m[1]<=col<=m[3]),(row,col,row,col))
            h=sum(rh(r) for r in range(host[0],host[2]+1));w=sum(cw(c) for c in range(host[1],host[3]+1))
            ox+=sum(cw(c) for c in range(host[1],col));oy+=sum(rh(r) for r in range(host[0],row))
            ext=d.find('x:ext',x)
            if ext is not None:cx=int(ext.attrib['cx']);cy=int(ext.attrib['cy'])
            else:
                to=d.find('x:to',x)
                if to is None:errors.append(f'Drawing {idx}: missing extent');continue
                to_row=int(to.find('x:row',x).text)+1;to_col=int(to.find('x:col',x).text)+1
                cx=sum(cw(c) for c in range(col,to_col))+int(to.find('x:colOff',x).text)-int(fr.find('x:colOff',x).text)
                cy=sum(rh(r) for r in range(row,to_row))+int(to.find('x:rowOff',x).text)-int(fr.find('x:rowOff',x).text)
            prefix=f'Drawing {idx} (Rows {host[0]}-{host[2]})'
            if cx<=0 or cy<=0:errors.append(prefix+': invalid extent')
            if ox<padding_x*9525-2 or w-ox-cx<padding_x*9525-2:errors.append(prefix+f': horizontal padding below {padding_x}px or overflow')
            if oy<padding_y*9525-2 or h-oy-cy<padding_y*9525-2:errors.append(prefix+f': vertical padding below {padding_y}px or overflow')
            if abs(2*ox+cx-w)>2 or abs(2*oy+cy-h)>2:errors.append(prefix+': image is not centered in its host')
    return errors

def check(path,sheet_name,expected,check_drawings=False,padding_x=14,padding_y=8,digit_width=7):
    with BoundedXlsx(path) as z:
        book=ET.fromstring(z.read('xl/workbook.xml'))
        sheets=book.findall('s:sheets/s:sheet',NS)
        sheet=next((s for s in sheets if s.attrib['name']==sheet_name),None) if sheet_name else sheets[0]
        if sheet is None:raise ValueError('Worksheet not found')
        relationships={r.attrib['Id']:r.attrib['Target'] for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
        target=relationships[sheet.attrib['{'+NS['r']+'}id']]
        member=target.lstrip('/') if target.startswith('/') else 'xl/'+target
        shared=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            shared=[''.join(n.itertext()) for n in ET.fromstring(z.read('xl/sharedStrings.xml'))]
        cells={c.attrib['r']:c for c in ET.fromstring(z.read(member)).findall('s:sheetData/s:row/s:c',NS)}
        def value(c):
            v=c.find('s:v',NS)
            if c.attrib.get('t')=='s':return shared[int(v.text)] if v is not None else None
            inline=c.find('s:is',NS)
            if inline is not None:return ''.join(inline.itertext())
            return v.text if v is not None else None
        errors=[]
        for ref,c in cells.items():
            text=value(c)
            if c.attrib.get('t')=='e':errors.append(f'{ref}: formula error {text}')
            if text and '待确认' in text:errors.append(f'{ref}: forbidden placeholder')
            if text and re.search(r'\d+\s*张收据|\d+\s*条明细|\d+\s*张实物照片',text):errors.append(f'{ref}: unwanted summary counts')
        totals={}
        for ref,want in expected.items():
            got=value(cells[ref]) if ref in cells else None
            totals[ref]=got
            if got is None:errors.append(f'{ref}: no cached result; recalculate with Excel or a formula-capable tool before verifying')
            elif abs(Decimal(got)-Decimal(str(want)))>Decimal('0.001'):errors.append(f'{ref}: expected {want}, found {got}')
        if check_drawings:
            errors.extend(check_drawing_geometry(z,member,ET.fromstring(z.read(member)),padding_x,padding_y,digit_width))
        return {'worksheet':sheet.attrib['name'],'expected_cells':totals,'errors':errors}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workbook',required=True,type=Path)
    parser.add_argument('--sheet')
    parser.add_argument('--expect',action='append',default=[],help='Example H45=3716; repeatable')
    parser.add_argument('--check-drawings',action='store_true',help='Verify all drawings fit strictly within their target cells')
    parser.add_argument('--padding-x',type=float,default=14,help='Required horizontal image padding in pixels')
    parser.add_argument('--padding-y',type=float,default=8,help='Required vertical image padding in pixels')
    parser.add_argument('--digit-width',type=float,default=7,help='Calibrated default-font digit width in pixels')
    parser.add_argument('--max-errors',type=int,default=10,help='Maximum representative errors in the summary (0..100)')
    parser.add_argument('--report',type=Path,help='New auxiliary JSON with every check result and error')
    add_summary_arguments(parser)
    args=parser.parse_args();expected={}
    if not 0 <= args.max_errors <= 100 or not 512 <= args.max_chars <= 5000:
        parser.error('max-errors must be 0..100; max-chars must be 512..5000')
    if not all(math.isfinite(v) and 0 <= v <= 1000 for v in (args.padding_x,args.padding_y)) or not math.isfinite(args.digit_width) or not 0 < args.digit_width <= 100:
        parser.error('Invalid padding or font digit width')
    for arg in args.expect:
        ref,number=arg.split('=',1);expected[ref.upper()]=Decimal(number)
    report=check(args.workbook,args.sheet,expected,check_drawings=args.check_drawings,padding_x=args.padding_x,padding_y=args.padding_y,digit_width=args.digit_width)
    report_path=args.report or args.workbook.parent / '.work_validation' / ('check-' + uuid4().hex + '.json')
    report['check_complete']=True
    report['status']='failed' if report['errors'] else 'passed'
    try:
        details_path=write_new_json(report_path,report,protected=[args.workbook])
        emit_summary({'worksheet':report['worksheet'],'expected_cells':report['expected_cells'],
                      'status':report['status'],'check_complete':True,'details_path':details_path,
                      **error_summary(report['errors'],args.max_errors)},args.max_chars)
    except ValueError as error:
        parser.error(str(error))
    raise SystemExit(1 if report['errors'] else 0)

if __name__=='__main__':main()
