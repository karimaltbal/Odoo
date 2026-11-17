# MCP historical XLSX chunk upload

The three new MCP tools are `historical_upload_start`, `historical_upload_chunk`, and `historical_upload_finish`.
They allow an MCP client **that has access to the actual XLSX bytes** to upload the workbook without using Odoo's UI.

1. Read the original XLSX bytes in the MCP client (do not reconstruct workbook content from a text description).
2. Calculate byte length and lowercase SHA-256 hex digest. Call `historical_upload_start` with filename, import_type, size_bytes, sha256.
3. Split bytes into 49,152-byte pieces. Base64-encode each piece independently and send it to `historical_upload_chunk` with the returned transfer_id and a zero-based index. Retry identical chunks safely.
4. Call `historical_upload_finish`. It verifies size and SHA-256, creates an internal import session, and returns session_id.
5. Call existing `historical_validate_xlsx`, then `historical_approve_xlsx` (requires approver role), then `historical_execute_xlsx` only after explicit confirmation.

**Claude Web caveat:** These tools do not make Claude's conversation attachment bytes available to the remote MCP connector. If Claude Web cannot access the original attachment bytes in its tool-calling context, the workflow remains blocked; this requires client-side capability or a compatible file bridge. Do not manually retype base64 or claim a transfer succeeded when it did not.

**Duplicate import caution:** Previously imported developer units must be checked before execution. Chunk upload never removes, reverses, or updates existing financial records.

**Deployment:** Upgrade the Odoo module and refresh the MCP connector tool list. Requires the existing historical import operator group and API key write access. Test in staging; no full Odoo runtime integration tests were run in this build.
