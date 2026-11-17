# Historical import from any new Claude Code session

The Odoo module hosts a credential-free, versioned upload client at:
`https://contact-v18-beta.odoo.com/mcp/historical/client.py`

The operator can attach an XLSX and ask Claude:

> Download the historical import client from https://contact-v18-beta.odoo.com/mcp/historical/client.py and use it to upload the attached workbook to Odoo Beta. Then validate, approve, execute and verify using Odoo_Contact MCP tools. Do not retry an uncertain upload; stop and report an error. No extra confirmation is needed for the attached workbook.

Claude steps:
1. Download the Python client from the URL above to a temporary file using requests; do not copy it into a message. Verify the downloaded content is Python, not an HTML login/error page.
2. Find the attached XLSX local path. Determine whether it is `contracted` or `reserved`; do not guess if ambiguous.
3. Run `python3 /tmp/historical_upload.py "<attached-file-path>" contracted` (or `reserved`). The Cloud Environment must have access to the Odoo domain and a configured Network Secret that injects `X-Historical-Upload-Key`.
4. Capture the `session_id` from JSON stdout. Call `historical_validate_xlsx`, `historical_approve_xlsx`, `historical_execute_xlsx`, and `historical_import_status` in order using that same ID.
5. Verify imported records and totals through available Odoo tools. Stop on any error; no speculative retries. A timeout may leave an uploaded session, so inspect status before any further action.

## Boundaries

- This endpoint distributes only public client source; **never** secrets or company records.
- It does **not** bypass permissions, MCP approvals, or Odoo financial business logic.
- Structural validation is not a financial integrity guarantee. Execute only on authorized staging/production databases and with appropriate permissions.
- This approach removes Setup Script and GitHub dependencies but Claude still needs the short URL instruction (or a persistent Claude skill/project instruction). It cannot automatically discover a client hosted by Odoo without being told where to fetch it.
- The client is currently configured for `contact-v18-beta.odoo.com`. Other projects require a separate target configuration and secret; do not silently reuse this target.
