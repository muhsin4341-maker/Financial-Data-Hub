"""
JWT authentication middleware.

Validates Authorization: Bearer <token> header.
Attaches tenant_id, user_id, role to request.state.

Milestone: M1-Step14
"""
# TODO M1-Step14: Implement JWTAuthMiddleware
# TODO M1-Step14: Extract tenant_id, user_id, role from JWT payload
# TODO M1-Step14: Attach to request.state for downstream use
# TODO M1-Step14: Raise UnauthorizedError on invalid/expired token
