"""Generate fixed Tencent operations; fresh exports and visual checks remain gates."""
import datetime as dt
import re
from pathlib import Path

from bounded_xlsx import file_hash
from ledger_io import read_json, write_new_json
from ledger_model import WorkbookView, column_number, decimal, digest
from ledger_state import locked_run, check_inputs, save, atomic_json
from ledger_local import verify_candidate
from ledger_photo import framed_photo


def snapshot_identity(path, config):
    view = WorkbookView(path, config)
    return digest([view.rows,view.cell_styles,view.row_dimensions,view.merges,view.pictures,view.columns_layout])


def verify_partial(state, export, operations, cursor):
    """Prove applied operations from a fresh full export before resuming any write."""
    config=state['plan']['config']
    before=WorkbookView(state['inputs']['workbook']['path'],config)
    after=WorkbookView(export,config)
    def cells(view):
        result={}
        for row,items in view.rows.items():
            for col,cell in items.items():
                value=cell.get('formula') or cell.get('value')
                if value not in (None,''):
                    result[(row-1,column_number(col)-1)]=('='+str(value).lstrip('=')) if cell.get('formula') else value
        return result
    expected=cells(before)
    images=[(p['row']-1,p['col']-1,p['sha256']) for p in before.pictures]
    for op in operations[:cursor]:
        args=op['args']
        if op['tool']=='sheet_insert_dimension':
            start,count=args['index'],args['count']
            changed={}
            for (row,col),value in expected.items():
                if isinstance(value,str) and value.startswith('='):
                    value=re.sub(r'(\$?[A-Z]{1,3}\$?)([1-9][0-9]*)',lambda m:m[1]+str(int(m[2])+count if int(m[2])>start else int(m[2])),value)
                changed[(row+count if row>=start else row,col)]=value
            expected=changed
            images=[(row+count if row>=start else row,col,sha) for row,col,sha in images]
        elif op['tool']=='sheet_set_range_value':
            for cell in args['values']:
                expected[(cell['row'],cell['col'])]=cell.get('formula',cell.get('number_value',cell.get('string_value')))
        elif op['tool']=='sheet_insert_image':
            images.append((args['row_index'],args['col_index'],op['image']['sha256']))
    actual=cells(after)
    if set(expected)!=set(actual):
        raise ValueError('Online cells changed outside the completed operations')
    for key,want in expected.items():
        got=actual[key]
        if str(want)==str(got):
            continue
        try:
            same=abs(decimal(want)-decimal(got))<=decimal('0.000001')
        except ValueError:
            same=False
        if not same:
            raise ValueError('Online value/formula changed outside the completed operations')
    if sorted(images)!=sorted((p['row']-1,p['col']-1,p['sha256']) for p in after.pictures):
        raise ValueError('Online picture state is not the completed operation state')
    inserted=any(op['tool']=='sheet_insert_dimension' for op in operations[:cursor])
    for old in before.rows:
        new=state['plan']['historical_row_map'][str(old)] if inserted else old
        if before.cell_styles.get(old,{})!=after.cell_styles.get(new,{}):
            raise ValueError('Historical online formatting changed; review before resuming')
    return True


def online_plan(run_dir):
    with locked_run(run_dir) as (directory,state):
        check_inputs(state)
        if state['online']['status'] != 'not_requested':
            raise ValueError('Online plan already exists; resume its checkpoint, never regenerate an uncertain insertion')
        plan = state['plan']
        target = plan['config'].get('online_target', {})
        if target.get('provider') != 'tencent_docs' or not target.get('file_id') or not target.get('sheet_id'):
            raise ValueError('Explicit Tencent target is required in project config')
        target = {key:target[key] for key in ('file_id','sheet_id')}
        view = WorkbookView(state['inputs']['workbook']['path'], plan['config'])
        columns = plan['config']['columns']
        # Guard all writable value columns, not image IDs or entire-month contents.
        start,end = plan['insert_before']-1, max(plan['insert_before'],plan['total_row'] or 0)-1
        left,right = min(column_number(c) for c in columns.values())-1, column_number(columns['scene'])-1
        if end-start > 2000 or right-left > 30:
            raise ValueError('Online batch exceeds the bounded read area; split the purchase date')
        area = dict(start_row=start,end_row=end,start_col=left,end_col=right)
        grid = {}
        for row,cells in view.rows.items():
            for col,cell in cells.items():
                value = cell.get('formula') or cell.get('value')
                if cell.get('formula') and not str(value).startswith('='):
                    value = '='+value
                if not cell.get('formula') and cell.get('type') not in ('s','inlineStr','str'):
                    try:
                        value = float(decimal(value))
                    except ValueError:
                        pass
                grid[f'{row-1},{column_number(col)-1}'] = value
        def selected():
            return {key:value for key,value in grid.items() if start<=int(key.split(',')[0])<=end and left<=int(key.split(',')[1])<=right and value not in (None,'')}
        ops=[]
        def add(tool,args,mutate=None,image=None):
            before=selected()
            if mutate:
                mutate()
            op={'tool':tool,'args':args,'range':area,'before':before,'after':selected()}
            if image:
                op['image']=image
            ops.append(op)
        def insert():
            changed={}
            for key,value in grid.items():
                row,col=map(int,key.split(','))
                changed[f'{row+plan["insert_count"] if row>=start else row},{col}']=value
            grid.clear();grid.update(changed)
        if plan['insert_count']:
            # Insertion read-back excludes moved formulas until the explicit formula
            # write; Excel/Tencent may adjust their references automatically.
            add('sheet_insert_dimension',dict(index=start,count=plan['insert_count'],direction='before',dimension_type='row'),insert)
            ops[-1]['range']={**area,'end_row':start+plan['insert_count']-1}
            ops[-1]['before']={k:v for k,v in ops[-1]['before'].items() if int(k.split(',')[0])<start+plan['insert_count']}
            ops[-1]['after']={}
        values=[]
        for entry in plan['rows']:
            for field,value in entry['values'].items():
                if value is None:
                    continue
                item={'row':entry['row']-1,'col':column_number(columns[field])-1}
                if field in ('quantity','unit_price','amount') or field=='label' and entry['role']=='detail':
                    item.update(value_type='NUMBER',number_value=float(decimal(value)))
                else:
                    item.update(value_type='STRING',string_value=str(value))
                values.append(item)
        values.extend(dict(row=f['row']-1,col=column_number(f['column'])-1,value_type='FORMULA',formula=f['formula']) for f in plan['formulas'])
        def set_values():
            for item in values:
                grid[f'{item["row"]},{item["col"]}']=item.get('formula',item.get('number_value',item.get('string_value')))
        if values:
            add('sheet_set_range_value',{'values':values},set_values)
        fmt=plan['config'].get('format',{})
        if not fmt.get('font') or not fmt.get('body_size_pt'):
            raise ValueError('Online writes require explicit template font and body_size_pt')
        styles=dict(bg_color='FFFFFFFF',bold=False,font_color='FF000000',font_family=fmt.get('font','Arial'),
                    font_size=fmt.get('body_size_pt',11),horizontal_align='center',vertical_align='center',wrap_text=True,
                    italic=False,strike_through=False,underline='none',number_format_pattern='General')
        for entry in plan['rows']:
            row=entry['row']-1
            add('sheet_set_cell_style',{**styles,'start_row':row,'end_row':row,'start_col':left,'end_col':column_number(columns['photo'])-1,'bold':entry['role']!='detail'})
            for field in ('amount','unit_price'):
                col=column_number(columns[field])-1
                add('sheet_set_cell_style',{**styles,'start_row':row,'end_row':row,'start_col':col,'end_col':col,'number_format_pattern':'0.00','bold':entry['role']!='detail'})
            if entry['role']!='detail':
                add('sheet_merge_cell',dict(start_row=row,end_row=row,start_col=column_number(columns['label'])-1,end_col=column_number(columns['amount'])-2,merge_type='all'))
        if plan['rows']:
            dimensions=[]
            for entry in plan['rows']:
                key='online_detail_height_px' if entry['role']=='detail' else 'online_summary_height_px'
                if key not in fmt:
                    raise ValueError('Online dimensions require an explicitly calibrated '+key)
                dimensions.append(dict(dimension_type='row',index=entry['row']-1,size=fmt[key]))
            add('sheet_set_dimension_size',dict(dimensions=dimensions))
        for group in plan['groups']:
            for photo in group['photos']:
                row,col=photo['start_row']-1,column_number(columns['photo'])-1
                if photo['end_row']>photo['start_row']:
                    add('sheet_merge_cell',dict(start_row=row,end_row=photo['end_row']-1,start_col=col,end_col=col,merge_type='all'))
                width=fmt.get('online_photo_width_px')
                if width is None:
                    raise ValueError('Calibrate online_photo_width_px against the live photo column before writing')
                height=fmt['online_detail_height_px']*(photo['end_row']-photo['start_row']+1)
                asset=framed_photo(photo['path'],directory/'online-assets'/f'{group["id"]}-{row}.png',width,height,
                                   fmt.get('padding_x_px',14),fmt.get('padding_y_px',8))
                add('sheet_insert_image',dict(row_index=row,col_index=col),image=asset)
        output={'schema':'purchase-ledger-tencent/v1','plan_hash':state['plan_hash'],'target':target,'operations':ops,
                'before_identity':snapshot_identity(state['inputs']['workbook']['path'],plan['config'])}
        write_new_json(directory/'online-plan.json',output)
        write_new_json(directory/'online-checkpoint.json',{'plan_hash':state['plan_hash'],'cursor':0,'in_flight':None})
        state['online']={'status':'pending','plan':str(directory/'online-plan.json'),'operations_hash':digest(output)}
        save(directory,state,'online_prepared')
        return state


def verify_online(run_dir, export, receipt):
    with locked_run(run_dir) as (directory,state):
        check_inputs(state)
        target=state['plan']['config'].get('online_target',{})
        proof=read_json(receipt)
        if proof.get('file_id')!=target.get('file_id') or proof.get('sha256')!=file_hash(export):
            raise ValueError('Export receipt does not identify this target and file')
        when=dt.datetime.fromisoformat(proof['completed_at'])
        if not when.tzinfo or not 0 <= (dt.datetime.now(dt.timezone.utc)-when).total_seconds() <= 300:
            raise ValueError('Online read-back export must be no older than five minutes')
        operations=read_json(directory/'online-plan.json')
        if digest(operations)!=state['online']['operations_hash']:
            raise ValueError('Online plan changed after preparation')
        assets={(op['args']['row_index']+1,op['image']['source_sha256']):op['image']['sha256']
                for op in operations['operations'] if op['tool']=='sheet_insert_image'}
        report=verify_candidate(state,export,online_assets=assets)
        atomic_json(directory/'online-check.json',report)
        if report['errors']:
            raise ValueError(f'{len(report["errors"])} online checks failed; inspect the private full report')
        state['online'].update(status='verified',export=str(Path(export).resolve()),sha256=file_hash(export),receipt=str(Path(receipt).resolve()))
        save(directory,state,'online_verified')
        return state
