"""
Audit log middleware — writes every request to audit_log table.

Engineering Spec Part 3, Section 12 — Audit Logging.
Append-only. Non-blocking (async write after response).

Milestone: M1-Step15
"""
# TODO M1-Step15: Implement AuditMiddleware
# TODO M1-Step15: Generate UUID v7 request_id, attach to request.state
# TODO M1-Step15: Write audit_log record: tenant_id, user_id, method, path, status, ip, timestamp
# TODO M1-Step15: Return X-Request-ID response header
