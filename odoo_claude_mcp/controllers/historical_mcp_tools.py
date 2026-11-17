import base64
import hashlib
import json
from odoo.exceptions import UserError, ValidationError
from odoo.addons.odoo_claude_mcp.controllers.mcp_common import register, text_result, ToolError, ACCESS_CREATE, ACCESS_ACTION

MODEL = 'mcp.historical.import.session'

def session(env, args):
    rec = env[MODEL].browse(int(args.get('session_id') or 0)).exists()
    if not rec: raise ToolError('Import session not found or not accessible')
    rec.check_access('read')
    if rec.company_id not in env.companies: raise ToolError('Company is not accessible')
    if rec.owner_id != env.user and not env.user.has_group('odoo_claude_mcp.group_historical_mcp_approve'):
        raise ToolError('Only session owner or approver may access this import')
    return rec

@register('historical_upload_xlsx','Upload a complete XLSX file encoded as base64. Max 10 MB. Requires Claude client file bytes.',{'type':'object','properties':{'filename':{'type':'string'},'import_type':{'type':'string','enum':['contracted','reserved']},'content_base64':{'type':'string'}},'required':['filename','import_type','content_base64']},requires=(MODEL,),access=ACCESS_CREATE)
def upload(env,args,key):
    if not env.user.has_group('odoo_claude_mcp.group_historical_mcp_import'): raise ToolError('Historical import operator permission required')
    filename=args['filename']
    if not filename.lower().endswith('.xlsx'): raise ToolError('Only .xlsx supported')
    encoded=args['content_base64']
    if len(encoded)>14_000_000: raise ToolError('File too large')
    try: raw=base64.b64decode(encoded,validate=True)
    except Exception: raise ToolError('Invalid base64 file')
    if len(raw)>10*1024*1024: raise ToolError('File too large')
    rec=env[MODEL].create({'name':filename,'filename':filename,'import_type':args['import_type'],'file':base64.b64encode(raw),'file_hash':hashlib.sha256(raw).hexdigest()})
    return text_result(json.dumps({'session_id':rec.id,'state':rec.state}))

@register('historical_validate_xlsx','Inspect workbook and run structural preflight without importing.',{'type':'object','properties':{'session_id':{'type':'integer'}},'required':['session_id']},requires=(MODEL,),access=ACCESS_CREATE)
def validate(env,args,key):
    rec=session(env,args); rec.check_access('write')
    try:
        return text_result(json.dumps(rec.action_validate(), ensure_ascii=False))
    except (UserError, ValidationError) as exc:
        return text_result(json.dumps({'success': False, 'session_id': rec.id,
            'error_type': type(exc).__name__, 'error_message': str(exc)}, ensure_ascii=False))

@register('historical_import_status','Read import session state and preflight report.',{'type':'object','properties':{'session_id':{'type':'integer'}},'required':['session_id']},requires=(MODEL,))
def status(env,args,key):
    rec=session(env,args)
    return text_result(json.dumps({'session_id':rec.id,'state':rec.state,'validation':json.loads(rec.validation_json or '{}'),'result':rec.execution_result}))

@register('historical_approve_xlsx','Approve validated workbook (requires Odoo approver group).',{'type':'object','properties':{'session_id':{'type':'integer'}},'required':['session_id']},requires=(MODEL,),access=ACCESS_ACTION)
def approve(env,args,key):
    rec=session(env,args); rec.check_access('write')
    try:
        rec.action_approve()
    except (UserError, ValidationError) as exc:
        return text_result(json.dumps({'success': False, 'session_id': rec.id,
            'error_type': type(exc).__name__, 'error_message': str(exc)}, ensure_ascii=False))
    return text_result('Workbook approved for execution: %s' % rec.id)

@register('historical_execute_xlsx','Import an approved historical XLSX via existing Odoo wizard; creates financial records. Requires explicit user approval.',{'type':'object','properties':{'session_id':{'type':'integer'}},'required':['session_id']},requires=(MODEL,),access=ACCESS_ACTION)
def execute(env,args,key):
    rec=session(env,args); rec.check_access('write')
    try:
        return text_result(json.dumps(rec.action_execute(), ensure_ascii=False))
    except (UserError, ValidationError) as exc:
        return text_result(json.dumps({'success': False, 'session_id': rec.id,
            'error_type': type(exc).__name__, 'error_message': str(exc)}, ensure_ascii=False))

@register('historical_list_sessions',
          'List recent historical Excel import sessions uploaded through Odoo. Use this after the user uploads a workbook in the Odoo Historical Excel Imports screen to find its session_id.',
          {'type':'object','properties':{'limit':{'type':'integer','minimum':1,'maximum':20}}},
          requires=(MODEL,))
def list_sessions(env, args, key):
    if not (env.user.has_group('odoo_claude_mcp.group_historical_mcp_import') or
            env.user.has_group('odoo_claude_mcp.group_historical_mcp_approve')):
        raise ToolError('Historical import permission required')
    limit = max(1, min(int(args.get('limit') or 10), 20))
    domain = [('company_id', 'in', env.companies.ids)]
    if not env.user.has_group('odoo_claude_mcp.group_historical_mcp_approve'):
        domain.append(('owner_id', '=', env.uid))
    records = env[MODEL].search(domain, order='id desc', limit=limit)
    return text_result(json.dumps([{
        'session_id': rec.id, 'filename': rec.filename or rec.name,
        'import_type': rec.import_type, 'state': rec.state,
        'created_at': str(rec.create_date),
    } for rec in records]))

# Chunk upload requires the MCP client to have actual workbook bytes available.
# It does not grant Claude Web access to conversation attachments by itself.
@register('historical_upload_start',
          'Start direct XLSX transfer. Compute file size and SHA-256 from the actual XLSX bytes; split into 49152-byte chunks. No Odoo screen required.',
          {'type': 'object', 'properties': {
              'filename': {'type': 'string'},
              'import_type': {'type': 'string', 'enum': ['contracted', 'reserved']},
              'size_bytes': {'type': 'integer', 'minimum': 1, 'maximum': 10485760},
              'sha256': {'type': 'string', 'description': 'Lowercase 64-character SHA-256 hex digest of original XLSX bytes'},
          }, 'required': ['filename', 'import_type', 'size_bytes', 'sha256']},
          requires=(MODEL,), access=ACCESS_CREATE)
def historical_upload_start(env, args, key):
    if not env.user.has_group('odoo_claude_mcp.group_historical_mcp_import'):
        raise ToolError('Historical import operator permission required')
    filename = args['filename']
    size = int(args['size_bytes'])
    digest = args['sha256']
    if not filename.lower().endswith('.xlsx') or '/' in filename or '\\' in filename:
        raise ToolError('Valid XLSX filename required')
    if size <= 0 or size > 10 * 1024 * 1024:
        raise ToolError('Workbook must be between 1 byte and 10MB')
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ToolError('Invalid SHA-256 digest')
    count = (size + 49151) // 49152
    transfer = env['mcp.historical.upload.transfer'].create({
        'name': filename, 'import_type': args['import_type'], 'expected_size': size,
        'expected_sha256': digest, 'chunk_count': count,
    })
    return text_result(json.dumps({'transfer_id': transfer.id, 'chunk_size_bytes': 49152, 'chunk_count': count}))


@register('historical_upload_chunk',
          'Upload one base64-encoded chunk from the ORIGINAL XLSX bytes. Chunks are zero-indexed; retries with identical data are safe.',
          {'type': 'object', 'properties': {
              'transfer_id': {'type': 'integer'}, 'index': {'type': 'integer', 'minimum': 0},
              'content_base64': {'type': 'string'},
          }, 'required': ['transfer_id', 'index', 'content_base64']},
          requires=(MODEL,), access=ACCESS_CREATE)
def historical_upload_chunk(env, args, key):
    rec = env['mcp.historical.upload.transfer'].browse(int(args['transfer_id'])).exists()
    if not rec:
        raise ToolError('Transfer not found')
    return text_result(json.dumps(rec.append_chunk(int(args['index']), args['content_base64'])))


@register('historical_upload_finish',
          'Assemble and verify all XLSX chunks, then create an import session automatically. Does NOT validate, approve, or execute financial import.',
          {'type': 'object', 'properties': {'transfer_id': {'type': 'integer'}}, 'required': ['transfer_id']},
          requires=(MODEL,), access=ACCESS_CREATE)
def historical_upload_finish(env, args, key):
    rec = env['mcp.historical.upload.transfer'].browse(int(args['transfer_id'])).exists()
    if not rec:
        raise ToolError('Transfer not found')
    return text_result(json.dumps(rec.finish()))
