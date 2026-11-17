# Historical XLSX upload workflow (Odoo 18)

## Why this change exists
Claude's MCP connector may expose tool arguments but not the original bytes of an XLSX attachment. The existing `historical_upload_xlsx` accepts base64 and is preserved for clients with actual file-byte access. The server cannot reconstruct an XLSX from Claude's spreadsheet summary or a client-private attachment ID.

## Reliable fallback
1. Upgrade `odoo_claude_mcp` and assign the Odoo user the `Claude Historical Import Operator` group.
2. In Odoo, open **Historical Excel Imports > Import Sessions > New**.
3. Enter a name, select **Contracted** or **Reserved**, attach the original `.xlsx`, and save.
4. In Claude, call `historical_list_sessions` to locate the newly created session ID (or supply the ID yourself).
5. Call `historical_validate_xlsx` and inspect the preflight report. It is **structural only**; it does not guarantee the historical wizard's business checks will pass.
6. Have an authorized approver call `historical_approve_xlsx` after reviewing the report.
7. Only after the user explicitly authorizes the financial import, call `historical_execute_xlsx`.

No changes are made to `units_historical_process`.

## Limitations
- This fallback requires one manual upload in Odoo. Direct attachment-to-MCP transfer requires a Claude-supported file-byte transport or an independently reachable, authorized file source; this module cannot invent that access.
- The Odoo session form intentionally does not expose an Execute button, keeping financial execution behind the MCP approval workflow.
- Run upgrade and end-to-end checks on staging before production. This ZIP has passed static syntax/XML checks but has not been run against a live Odoo 18 database.
