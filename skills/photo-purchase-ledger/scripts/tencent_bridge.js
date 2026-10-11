/* Host adapter: pass the current connected tool caller and private file I/O.
 * No credentials, shell commands, arbitrary tool names or URLs are stored here. */
'use strict';

function response(result) {
  if (result.isError) throw new Error('Connected tool failed');
  const data = result.structuredContent || JSON.parse(result.content.find(x => x.type === 'text').text);
  if (data.error) throw new Error('Connected tool reported an error');
  return data;
}

async function exportWorkbook({call, download, sleep, fileId}) {
  const task = response(await call('manage_export_file', {file_id:fileId}));
  if (!task.task_id) throw new Error('Export did not return a task');
  for (let i=0; i<40; i++) {
    await sleep(3000);
    const result = response(await call('manage_export_progress', {task_id:task.task_id}));
    if (result.progress === 100) {
      if (typeof result.file_url !== 'string') throw new Error('Export URL missing');
      // The downloader must enforce HTTPS, a selected host, size limits and NEW output.
      return await download(result.file_url, {file_id:fileId, task_id:task.task_id});
    }
  }
  throw new Error('Export timed out; an older export cannot replace it');
}

const allowed = new Set(['sheet_insert_dimension','sheet_set_range_value','sheet_set_cell_style',
  'sheet_set_dimension_size','sheet_merge_cell','sheet_insert_image']);

function verifyCells(actual, expected, range) {
  const cells = new Map((actual.cells || []).map(c => [`${c.row},${c.col}`, c]));
  for (let row=range.start_row; row<=range.end_row; row++) {
    for (let col=range.start_col; col<=range.end_col; col++) {
      const key = `${row},${col}`, cell = cells.get(key) || {}, want = expected[key] ?? null;
      if (cell.value_type === 'ERROR') throw new Error('Read-back contains a formula error');
      let value = cell.formula || (cell.value_type === 'NUMBER' ? cell.number_value : cell.value_type === 'BOOL' ? cell.bool_value : cell.string_value ?? null);
      if (value === '') value = null;
      if (typeof want === 'number' && typeof value === 'number' && Math.abs(value-want)<=0.000001) continue;
      if (String(value ?? '') !== String(want ?? '')) throw new Error(`Read-back conflict at row ${row+1}, col ${col+1}`);
    }
  }
}

async function applyOperations({call, plan, checkpoint, saveCheckpoint, loadImage, refresh, saveReadback}) {
  if (!plan.target.file_id || !plan.target.sheet_id || !plan.plan_hash) throw new Error('Explicit target and plan required');
  if (checkpoint.plan_hash !== plan.plan_hash) throw new Error('Checkpoint belongs to another plan');
  // A callback must re-export and compare the entire current workbook, including
  // collaborator changes. A timestamp or total alone is insufficient.
  const refreshed = await refresh(checkpoint.cursor,checkpoint.in_flight);
  if (refreshed && refreshed.checkpoint) Object.assign(checkpoint,refreshed.checkpoint);
  if (checkpoint.in_flight !== null) throw new Error('An operation has an uncertain result; inspect a fresh export before continuing');
  for (let i=checkpoint.cursor; i<plan.operations.length; i++) {
    const op = plan.operations[i];
    if (!allowed.has(op.tool)) throw new Error('Unsupported operation');
    const args = {...op.args,...plan.target};
    const readArgs = {...op.range,...plan.target,return_csv:false,include_formula:true};
    const before = response(await call('sheet_get_cell_data', readArgs));
    verifyCells(before, op.before, op.range);
    checkpoint.in_flight = i;
    await saveCheckpoint(checkpoint);
    if (op.tool === 'sheet_insert_image') args.content = await loadImage(op.image);
    response(await call(op.tool,args));
    const after = response(await call('sheet_get_cell_data',readArgs));
    verifyCells(after,op.after,op.range);
    await saveReadback(i,after);
    checkpoint.cursor = i+1;
    checkpoint.in_flight = null;
    await saveCheckpoint(checkpoint);
  }
  return {status:'submitted',operations:checkpoint.cursor,acceptance_verified:false};
}

async function loadImageParts(readPart, image) {
  let content='', total=1;
  while(content.length<total) {
    const part=await readPart({...image,offset:content.length});
    if (!part.content || part.total_chars>140000000) throw new Error('Invalid bounded image payload');
    content+=part.content;
    total=part.total_chars;
  }
  return content;
}

if (typeof module !== 'undefined') module.exports = {exportWorkbook,applyOperations,verifyCells,loadImageParts};
