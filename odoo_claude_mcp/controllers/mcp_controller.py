# -*- coding: utf-8 -*-
import json
import logging
import uuid
from contextlib import closing
from datetime import timedelta

from markupsafe import escape

from odoo import fields, http
from odoo.http import request

from . import mcp_tools
from .mcp_common import ACCESS_READ, ODOO_VERSION, ToolError, tool_access

_logger = logging.getLogger(__name__)


def _module_version():
    try:
        from odoo.modules.module import get_manifest
        return get_manifest('odoo_claude_mcp').get('version') or 'unknown'
    except Exception:  # noqa: BLE001 - informational only
        return 'unknown'


def _scoped_env(user):
    """Environment the tools run in: the key's user (never sudo), the user's
    time zone and language, and ALL companies the user may access, with the
    user's default company first (it is the currency used by sale.report)."""
    company_ids = [user.company_id.id] + [c for c in user.company_ids.ids if c != user.company_id.id]
    context = dict(
        request.env.context,
        tz=user.tz or user.company_id.partner_id.tz or 'UTC',
        lang=user.lang,
        allowed_company_ids=company_ids,
    )
    return request.env(user=user.id, context=context, su=False)

SUPPORTED_MCP_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

SERVER_INSTRUCTIONS = (
    "Access to this Odoo {version} database. For ANY data question, even if no dedicated "
    "tool exists: (1) find_fields or list_models to locate the right model and real field names; "
    "(2) describe_model when you need a model's full field list; (3) query_data for records or "
    "aggregates; (4) multi_query to run several related queries in one call; (5) get_record with "
    "`expand` to see a document and its lines. Never invent field names. "
    "ACCURACY RULES: for totals, counts, rankings, trends and period comparisons ALWAYS use "
    "query_data in aggregate mode (group_by + measures) - it is computed by the database over ALL "
    "matching rows. Never add up records you listed yourself, because listings are capped. "
    "For a year-over-year or month-by-month comparison use ONE aggregate query grouped by a date "
    "field, e.g. group_by=['date_order:year'] or ['date_order:month']; date groups come back in "
    "chronological order. Use count_distinct (e.g. partner_id) for 'how many distinct ...', and "
    "`having` to filter on totals. "
    "sale.order.amount_total is in each order's own currency (group by currency_id if several are "
    "used); sale.report gives confirmed sales converted to the company currency, with quantities "
    "in the product's unit of measure. Confirmed sales are state in ['sale','done']. "
    "TIME: every date you send and every date shown is in the user's time zone ({tz}); a plain "
    "date on a datetime field means the whole local day. Use date tokens such as @start_of_year, "
    "@start_of_month, @start_of_last_month, @months_ago:3 instead of guessing today's date. "
    "COMPANIES: data covers all companies of the user ({companies}); group or filter by "
    "company_id to separate them. Many2one values are shown as 'Name [id]' - filter on the id "
    "for exact matches. "
    "If any result says it was truncated, capped or incomplete, do NOT present it as a final "
    "figure: re-run it as an aggregate, with narrower filters, or with `offset` for the next page. "
    "If a model is not available, say so and tell the user an administrator can add it under "
    "Queryable Models. "
    "URLS: results include a direct Odoo URL per record; give the user the URL of every record "
    "you mention, created or changed. "
    "{write_rules}"
    "Answer in the language the user writes in."
)

WRITE_RULES = {
    'read': "This connection is READ-ONLY: you cannot create or change records; say so if asked. ",
    'create': (
        "WRITING: you may create DRAFT records with create_record / create_records_bulk, but only when "
        "the user explicitly asks. Check the fields with describe_model first (fields marked required "
        "must be set). For spreadsheet data, map the columns to real field names and confirm the "
        "mapping with the user when it is not obvious. Business actions (confirm, validate...) are "
        "NOT allowed on this connection; the user confirms the drafts in Odoo. "),
    'full': (
        "WRITING: you may create DRAFT records with create_record / create_records_bulk, but only when "
        "the user explicitly asks. Check the fields with describe_model first (fields marked required "
        "must be set). For spreadsheet data, map the columns to real field names and confirm the "
        "mapping with the user when it is not obvious. execute_action changes the business state "
        "(confirm, post, validate, won/lost, cancel): run it ONLY when the user explicitly asks for "
        "those specific records - never chain it automatically after creating records - and report "
        "each record's result with its URL. "),
}

ACCESS_TOKEN_TTL_SECONDS = 3600  # 1 hour
MAX_LOGGED_ARGUMENTS = 20000      # bulk imports can be large
AUTH_CODE_TTL_SECONDS = 300      # 5 minutes


def _json_response(payload, status=200):
    return request.make_response(
        json.dumps(payload),
        status=status,
        headers=[('Content-Type', 'application/json')],
    )


def _jsonrpc_error(id_, code, message):
    return {
        'jsonrpc': '2.0',
        'id': id_,
        'error': {'code': code, 'message': message},
    }


def _jsonrpc_result(id_, result):
    return {'jsonrpc': '2.0', 'id': id_, 'result': result}


def _base_url():
    return request.httprequest.url_root.rstrip('/')


class MCPController(http.Controller):

    # OAuth discovery (RFC 9728 / RFC 8414)
    @http.route('/.well-known/oauth-protected-resource', type='http',
                auth='none', methods=['GET'], csrf=False)
    def oauth_protected_resource(self, **kwargs):
        """Tells the MCP client (claude.ai) which Authorization Server is
        responsible for issuing tokens accepted by our /mcp endpoint."""
        base = _base_url()
        return _json_response({
            'resource': f'{base}/mcp',
            'authorization_servers': [base],
        })

    @http.route('/.well-known/oauth-authorization-server', type='http',
                auth='none', methods=['GET'], csrf=False)
    def oauth_authorization_server(self, **kwargs):
        base = _base_url()
        return _json_response({
            'issuer': base,
            'authorization_endpoint': f'{base}/mcp/oauth/authorize',
            'token_endpoint': f'{base}/mcp/oauth/token',
            'registration_endpoint': f'{base}/mcp/oauth/register',
            'response_types_supported': ['code'],
            'grant_types_supported': ['authorization_code', 'refresh_token'],
            'code_challenge_methods_supported': ['S256'],
            'token_endpoint_auth_methods_supported': ['none'],
        })

    # Dynamic Client Registration (RFC 7591)
    @http.route('/mcp/oauth/register', type='http', auth='none',
                methods=['POST'], csrf=False)
    def oauth_register(self, **kwargs):
        try:
            body = json.loads(request.httprequest.data or b'{}')
        except ValueError:
            return _json_response({'error': 'invalid_client_metadata'}, 400)

        redirect_uris = body.get('redirect_uris') or []
        if not redirect_uris:
            return _json_response({'error': 'invalid_redirect_uri'}, 400)

        client_id = uuid.uuid4().hex
        request.env['mcp.oauth.client'].sudo().create({
            'client_id': client_id,
            'client_name': body.get('client_name', 'Unnamed MCP Client'),
            'redirect_uris': json.dumps(redirect_uris),
            'token_endpoint_auth_method': 'none',
        })
        return _json_response({
            'client_id': client_id,
            'redirect_uris': redirect_uris,
            'token_endpoint_auth_method': 'none',
            'grant_types': ['authorization_code', 'refresh_token'],
            'response_types': ['code'],
        }, 201)

    # Authorization endpoint - simplified "consent screen"
    @http.route('/mcp/oauth/authorize', type='http', auth='none',
                methods=['GET'], csrf=False)
    def oauth_authorize_form(self, **kwargs):
        client_id = kwargs.get('client_id', '')
        redirect_uri = kwargs.get('redirect_uri', '')
        state = kwargs.get('state', '')
        code_challenge = kwargs.get('code_challenge', '')
        code_challenge_method = kwargs.get('code_challenge_method', 'S256')

        client = request.env['mcp.oauth.client'].sudo().search(
            [('client_id', '=', client_id)], limit=1)
        if not client or not client._redirect_uri_allowed(redirect_uri):
            return request.make_response(
                '<h3>Invalid client_id or redirect_uri.</h3>', status=400)

        # Minimal, dependency-free HTML form. The admin pastes the raw
        # mcp_xxx key generated earlier from Settings > Claude MCP > API Keys.
        client_id, redirect_uri, state = escape(client_id), escape(redirect_uri), escape(state)
        code_challenge = escape(code_challenge)
        code_challenge_method = escape(code_challenge_method)
        html = f"""
        <html><body style="font-family:sans-serif;max-width:420px;margin:60px auto;">
          <h3>Connect Claude to Odoo</h3>
          <p>Paste your MCP API Key to authorize this connection.</p>
          <form method="POST" action="/mcp/oauth/authorize">
            <input type="hidden" name="client_id" value="{client_id}"/>
            <input type="hidden" name="redirect_uri" value="{redirect_uri}"/>
            <input type="hidden" name="state" value="{state}"/>
            <input type="hidden" name="code_challenge" value="{code_challenge}"/>
            <input type="hidden" name="code_challenge_method" value="{code_challenge_method}"/>
            <input type="password" name="api_key" placeholder="mcp_..."
                   style="width:100%;padding:8px;margin:12px 0;" required/>
            <button type="submit" style="padding:8px 16px;">Authorize</button>
          </form>
        </body></html>
        """
        return request.make_response(html, headers=[('Content-Type', 'text/html')])

    @http.route('/mcp/oauth/authorize', type='http', auth='none',
                methods=['POST'], csrf=False)
    def oauth_authorize_submit(self, **post):
        client_id = post.get('client_id', '')
        redirect_uri = post.get('redirect_uri', '')
        state = post.get('state', '')
        code_challenge = post.get('code_challenge', '')
        code_challenge_method = post.get('code_challenge_method', 'S256')
        raw_api_key = post.get('api_key', '')

        client = request.env['mcp.oauth.client'].sudo().search(
            [('client_id', '=', client_id)], limit=1)
        api_key_rec = request.env['mcp.api.key'].sudo()._authenticate(raw_api_key)

        if not client or not api_key_rec:
            return request.make_response(
                '<h3>Invalid API Key. Go back and try again.</h3>', status=401)

        code = uuid.uuid4().hex + uuid.uuid4().hex
        request.env['mcp.oauth.code'].sudo().create({
            'code': code,
            'client_id_ref': client.id,
            'api_key_id': api_key_rec.id,
            'redirect_uri': redirect_uri,
            'code_challenge': code_challenge,
            'code_challenge_method': code_challenge_method,
            'expires_at': fields.Datetime.now() + timedelta(seconds=AUTH_CODE_TTL_SECONDS),
        })
        separator = '&' if '?' in redirect_uri else '?'
        location = f'{redirect_uri}{separator}code={code}&state={state}'
        return request.redirect(location, local=False)

    # Token endpoint
    @http.route('/mcp/oauth/token', type='http', auth='none',
                methods=['POST'], csrf=False)
    def oauth_token(self, **post):
        grant_type = post.get('grant_type')
        OauthToken = request.env['mcp.oauth.token'].sudo()

        if grant_type == 'authorization_code':
            code_val = post.get('code')
            code_verifier = post.get('code_verifier', '')
            code_rec = request.env['mcp.oauth.code'].sudo().search(
                [('code', '=', code_val), ('used', '=', False)], limit=1)
            if not code_rec or code_rec.expires_at < fields.Datetime.now():
                return _json_response({'error': 'invalid_grant'}, 400)
            if post.get('redirect_uri') != code_rec.redirect_uri:
                return _json_response({'error': 'invalid_grant'}, 400)
            if not code_rec._verify_pkce(code_verifier):
                return _json_response({'error': 'invalid_grant'}, 400)

            code_rec.used = True
            raw_access = OauthToken._new_raw_token()
            raw_refresh = OauthToken._new_raw_token()
            OauthToken.create({
                'access_token_hash': OauthToken._hash_token(raw_access),
                'refresh_token_hash': OauthToken._hash_token(raw_refresh),
                'client_id_ref': code_rec.client_id_ref.id,
                'api_key_id': code_rec.api_key_id.id,
                'access_expires_at': fields.Datetime.now() + timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS),
            })
            return _json_response({
                'access_token': raw_access,
                'token_type': 'Bearer',
                'expires_in': ACCESS_TOKEN_TTL_SECONDS,
                'refresh_token': raw_refresh,
            })

        elif grant_type == 'refresh_token':
            raw_refresh = post.get('refresh_token', '')
            token_rec = OauthToken.search(
                [('refresh_token_hash', '=', OauthToken._hash_token(raw_refresh)),
                 ('revoked', '=', False)], limit=1)
            if not token_rec:
                return _json_response({'error': 'invalid_grant'}, 400)

            raw_access = OauthToken._new_raw_token()
            raw_refresh_new = OauthToken._new_raw_token()
            token_rec.write({
                'access_token_hash': OauthToken._hash_token(raw_access),
                'refresh_token_hash': OauthToken._hash_token(raw_refresh_new),
                'access_expires_at': fields.Datetime.now() + timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS),
            })
            return _json_response({
                'access_token': raw_access,
                'token_type': 'Bearer',
                'expires_in': ACCESS_TOKEN_TTL_SECONDS,
                'refresh_token': raw_refresh_new,
            })

        return _json_response({'error': 'unsupported_grant_type'}, 400)

    # The actual MCP endpoint (Streamable HTTP - JSON-RPC 2.0 over POST)
    def _authenticate_bearer(self):
        auth_header = request.httprequest.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return None
        raw_token = auth_header[len('Bearer '):].strip()
        OauthToken = request.env['mcp.oauth.token'].sudo()
        token_hash = OauthToken._hash_token(raw_token)
        token_rec = OauthToken.search([
            ('access_token_hash', '=', token_hash),
            ('revoked', '=', False),
        ], limit=1)
        if not token_rec or token_rec.access_expires_at < fields.Datetime.now():
            return None
        if not token_rec.api_key_id.active:
            return None
        return token_rec.api_key_id

    def _handle_tools_call(self, msg_id, body, api_key_rec):
        params = body.get('params') or {}
        tool_name = params.get('name')
        arguments = params.get('arguments') or {}

        acting_user = api_key_rec.user_id
        if not acting_user or not acting_user.active:
            return _json_response(
                _jsonrpc_error(msg_id, -32000, 'API key is not linked to an active user'))

        scoped_env = _scoped_env(acting_user)

        log_vals = {
            'api_key_id': api_key_rec.id,
            'user_id': acting_user.id,
            'method': 'tools/call',
            'tool_name': tool_name,
            'arguments': json.dumps({k: ('[REDACTED FILE CONTENT]' if k in ('content_base64', 'file_base64') else v) for k, v in arguments.items()}, ensure_ascii=False)[:MAX_LOGGED_ARGUMENTS],
            'access': tool_access(tool_name),
        }

        # Layer 1: is this tool allowed for this API key at all?
        if not api_key_rec._tool_allowed(tool_name):
            log_vals.update(success=False, error_message='Tool not permitted for this key')
            self._write_log(log_vals)
            return _json_response(_jsonrpc_result(msg_id, {
                'content': [{'type': 'text',
                             'text': f"The tool '{tool_name}' is not permitted for this API key."}],
                'isError': True,
            }))

        try:
            # Read tools run inside a savepoint that is ALWAYS rolled back:
            # - a database error (bad SQL, serialization failure, ...) no longer
            #   leaves the transaction aborted, so the error can still be logged;
            # - nothing a read tool might write (directly or through a computed
            #   field) can ever be committed, which enforces the read-only promise.
            if tool_access(tool_name) == ACCESS_READ:
                with closing(request.env.cr.savepoint()):
                    result = mcp_tools.call_tool(scoped_env, tool_name, arguments, api_key_rec)
            else:
                # Write tools: kept on success, rolled back on any error (all or nothing
                # for the call; bulk tools use inner savepoints per record).
                with request.env.cr.savepoint():
                    result = mcp_tools.call_tool(scoped_env, tool_name, arguments, api_key_rec)
        except ToolError as e:
            log_vals.update(success=False, error_message=str(e))
            self._write_log(log_vals)
            return _json_response(_jsonrpc_result(msg_id, {
                'content': [{'type': 'text', 'text': str(e)}],
                'isError': True,
            }))
        except Exception as e:  # noqa: BLE001 - deliberately broad, logged below
            _logger.exception("MCP tool '%s' raised an unexpected error", tool_name)
            log_vals.update(success=False, error_message=f"{type(e).__name__}: {e}")
            self._write_log(log_vals)
            return _json_response(_jsonrpc_result(msg_id, {
                'content': [{'type': 'text', 'text':
                             f"The tool '{tool_name}' failed with an internal error "
                             f"({type(e).__name__}). Try a simpler or narrower query."}],
                'isError': True,
            }))

        if result is None:
            log_vals.update(success=False, error_message='Unknown tool')
            self._write_log(log_vals)
            return _json_response(
                _jsonrpc_error(msg_id, -32602, f"Unknown tool: {tool_name}"))

        log_vals['result_summary'] = json.dumps(result, ensure_ascii=False)[:2000]
        self._write_log(log_vals)
        return _json_response(_jsonrpc_result(msg_id, result))

    def _write_log(self, log_vals):
        """Write the call log in its own savepoint: a logging failure must never
        hide the tool result from the client."""
        try:
            with request.env.cr.savepoint():
                request.env['mcp.tool.log'].sudo().create(log_vals)
        except Exception:  # noqa: BLE001
            _logger.exception("Could not write the MCP call log")

    def _instructions(self, api_key_rec):
        user = api_key_rec.user_id
        write_rules = WRITE_RULES.get(api_key_rec.write_mode or 'read', WRITE_RULES['read'])
        if not user:
            return SERVER_INSTRUCTIONS.format(version=ODOO_VERSION, tz='UTC', companies='-',
                                              write_rules=write_rules)
        env = _scoped_env(user)
        companies = ", ".join(f"{c.name} [{c.id}]" for c in env.companies)
        return SERVER_INSTRUCTIONS.format(
            version=ODOO_VERSION, tz=env.context.get('tz'), companies=companies, write_rules=write_rules)

    @http.route('/mcp', type='http', auth='none', methods=['POST', 'GET'], csrf=False)
    def mcp_endpoint(self, **kwargs):
        if request.httprequest.method == 'GET':
            return request.make_response(
                json.dumps({'error': 'method_not_allowed',
                            'message': 'This endpoint only supports POST (Streamable HTTP).'}),
                status=405,
                headers=[('Content-Type', 'application/json'), ('Allow', 'POST')],
            )

        base = _base_url()
        api_key_rec = self._authenticate_bearer()
        if not api_key_rec:
            www_auth = (
                f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
            )
            return request.make_response(
                json.dumps({'error': 'unauthorized'}),
                status=401,
                headers=[
                    ('Content-Type', 'application/json'),
                    ('WWW-Authenticate', www_auth),
                ],
            )

        try:
            body = json.loads(request.httprequest.data or b'{}')
        except ValueError:
            return _json_response(_jsonrpc_error(None, -32700, 'Parse error'), 400)

        if not isinstance(body, dict):
            return _json_response(_jsonrpc_error(None, -32600, 'Invalid request: expected one JSON-RPC object'), 400)

        method = body.get('method')
        msg_id = body.get('id')

        is_notification = 'id' not in body

        if method == 'initialize':
            client_version = (body.get('params') or {}).get('protocolVersion')
            negotiated = client_version if client_version in SUPPORTED_MCP_VERSIONS \
                else SUPPORTED_MCP_VERSIONS[0]
            result = {
                'protocolVersion': negotiated,
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'odoo-claude-mcp-server', 'version': _module_version()},
                'instructions': self._instructions(api_key_rec),
            }
            return _json_response(_jsonrpc_result(msg_id, result))

        if method == 'notifications/initialized':
            # Client confirming the handshake is complete. Nothing to do.
            return request.make_response('', status=202)

        if method == 'tools/list':
            tools = mcp_tools.visible_tool_definitions(
                request.env, api_key_rec
            )
            _logger.debug('MCP TOOLS/LIST | db=%s | api_key_id=%s | total_tools=%s',
                          request.env.cr.dbname, api_key_rec.id, len(tools))
            return _json_response(_jsonrpc_result(msg_id, {'tools': tools}))

        if method == 'tools/call':
            return self._handle_tools_call(msg_id, body, api_key_rec)

        if is_notification:
            return request.make_response('', status=202)

        return _json_response(_jsonrpc_error(msg_id, -32601, 'Method not found'))
