# Claude Web direct XLSX upload (Odoo 18)

This endpoint transfers the **original binary workbook** from Claude's Python environment to Odoo. It creates an internal staging session only. It never approves or executes financial imports.

## One-time Odoo administrator setup

Deploy/upgrade `odoo_claude_mcp`, then run this in **Odoo Shell**. Use a dedicated active user with the `Claude Historical Import Operator` group; the user should have only the companies needed for the import. Replace `OPERATOR_USER_ID` with that user's numeric ID.

```python
import secrets, hashlib
operator_id = OPERATOR_USER_ID
operator = env['res.users'].sudo().browse(operator_id).exists()
assert operator and operator.active
assert operator.has_group('odoo_claude_mcp.group_historical_mcp_import')
secret = secrets.token_urlsafe(48)
settings = env['ir.config_parameter'].sudo()
settings.set_param('odoo_claude_mcp.direct_upload_token_sha256', hashlib.sha256(secret.encode()).hexdigest())
settings.set_param('odoo_claude_mcp.direct_upload_user_id', str(operator_id))
print('ONE-TIME UPLOAD TOKEN (store securely; never paste into a chat):', secret)
```

Keep the token in a secret-management facility accessible to Claude's Python execution environment, if supported. Do not paste it into conversation text, prompts, repositories or logs. The token is not the MCP OAuth token. Rotating the token means repeating setup with a new secret. To disable, clear `odoo_claude_mcp.direct_upload_token_sha256`.

**Credential limitation:** If Claude Web does not provide a secure way to inject this secret into Python Code Execution, this design cannot be operated safely without a separate credential broker. Never ask Claude to guess or extract connector credentials.

## Python client (run in Claude Web Code Execution)

The original attachment must exist as a real local file in the execution environment. Set `ODOO_HISTORICAL_UPLOAD_KEY` via a supported secrets mechanism; never embed the token in the script.

```python
import os, hashlib, requests
from pathlib import Path

file_path = Path('/path/to/original.xlsx')
raw = file_path.read_bytes()
key = os.environ['ODOO_HISTORICAL_UPLOAD_KEY']
response = requests.post(
    'https://contact-v18-beta.odoo.com/mcp/historical/upload',
    data=raw,
    headers={
        'X-Historical-Upload-Key': key,
        'X-Filename': file_path.name,
        'X-Import-Type': 'contracted',
        'X-Content-SHA256': hashlib.sha256(raw).hexdigest(),
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    }, timeout=90,
)
response.raise_for_status()
result = response.json()
assert result['sha256'] == hashlib.sha256(raw).hexdigest()
print({'session_id': result['session_id'], 'state': result['state']})
```

Then use `historical_validate_xlsx` with the returned session ID via the Odoo MCP connector. Review any duplicate unit and financial-record risks before separately approving and executing. Validation is structural only and **does not** establish duplicate safety.

## Testing

1. `POST` without a token -> 401.
2. `POST` with invalid token -> 401.
3. Wrong file SHA-256 -> 422, no session.
4. Valid XLSX with valid token -> 201 and session ID, no financial records created.
5. Validate via MCP, confirm explicit approval is still required.
6. Rotate/revoke the upload token and confirm old token is rejected.

Do not execute a repeat import for an already imported unit until its business-level duplicate handling has been verified.
