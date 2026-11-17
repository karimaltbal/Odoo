# -*- coding: utf-8 -*-
import base64
import hashlib
import secrets

from odoo import api, fields, models


def _hash(raw):
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


class McpOauthClient(models.Model):
    _name = 'mcp.oauth.client'
    _description = 'MCP OAuth Client (Dynamically Registered)'

    client_id = fields.Char(required=True, index=True, copy=False)
    client_name = fields.Char()
    redirect_uris = fields.Text(
        help="JSON-encoded list of allowed redirect URIs, provided by the "
             "client at registration time."
    )
    token_endpoint_auth_method = fields.Char(default='none')

    def _redirect_uri_allowed(self, redirect_uri):
        import json
        try:
            uris = json.loads(self.redirect_uris or '[]')
        except ValueError:
            uris = []
        return redirect_uri in uris


class McpOauthCode(models.Model):
    _name = 'mcp.oauth.code'
    _description = 'MCP OAuth Authorization Code'

    code = fields.Char(required=True, index=True, copy=False)
    client_id_ref = fields.Many2one('mcp.oauth.client', required=True)
    api_key_id = fields.Many2one('mcp.api.key', required=True)
    redirect_uri = fields.Char(required=True)
    code_challenge = fields.Char(required=True)
    code_challenge_method = fields.Char(default='S256')
    expires_at = fields.Datetime(required=True)
    used = fields.Boolean(default=False)

    def _verify_pkce(self, code_verifier):
        self.ensure_one()
        if self.code_challenge_method != 'S256':
            return False
        digest = hashlib.sha256(code_verifier.encode('utf-8')).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
        return secrets.compare_digest(computed, self.code_challenge)

    @api.model
    def _cron_cleanup_expired(self):
        self.search([('expires_at', '<', fields.Datetime.now())]).unlink()
        self.env['mcp.oauth.token'].search([('revoked', '=', True)]).unlink()


class McpOauthToken(models.Model):
    """Access + refresh token pair handed to claude.ai after a successful
    code exchange. We store only hashes, never the raw secrets."""
    _name = 'mcp.oauth.token'
    _description = 'MCP OAuth Access/Refresh Token'

    access_token_hash = fields.Char(required=True, index=True)
    refresh_token_hash = fields.Char(required=True, index=True)
    client_id_ref = fields.Many2one('mcp.oauth.client', required=True)
    api_key_id = fields.Many2one('mcp.api.key', required=True)
    access_expires_at = fields.Datetime(required=True)
    revoked = fields.Boolean(default=False)

    @staticmethod
    def _new_raw_token():
        return secrets.token_urlsafe(48)

    @classmethod
    def _hash_token(cls, raw):
        return _hash(raw)
