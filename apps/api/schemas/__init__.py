# Pydantic request/response schemas — added per milestone
#
# M1: auth (RegisterRequest, LoginRequest, ForgotPasswordRequest,
#           ResetPasswordRequest, MessageResponse, AuthResponse)
# M2: companies (CompanyCreate, CompanyUpdate, CompanyResponse, CompanyListResponse)
#     jobs      (JobCreate, JobUpdate, JobResponse, JobListResponse, JobStatusResponse)
#     invitations (InvitationCreate, InvitationResponse)

from apps.api.schemas.auth import (
    AuthResponse,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    ResetPasswordRequest,
)
from apps.api.schemas.companies import (
    CompanyCreate,
    CompanyListResponse,
    CompanyResponse,
    CompanyUpdate,
)
from apps.api.schemas.invitations import (
    InvitationCreate,
    InvitationResponse,
)
from apps.api.schemas.jobs import (
    JobCreate,
    JobListResponse,
    JobResponse,
    JobStatusResponse,
    JobUpdate,
)

__all__ = [
    # Auth (M1)
    "AuthResponse",
    "ForgotPasswordRequest",
    "LoginRequest",
    "MessageResponse",
    "RegisterRequest",
    "ResetPasswordRequest",
    # Companies (M2)
    "CompanyCreate",
    "CompanyUpdate",
    "CompanyResponse",
    "CompanyListResponse",
    # Jobs (M2)
    "JobCreate",
    "JobUpdate",
    "JobResponse",
    "JobListResponse",
    "JobStatusResponse",
    # Invitations (M2)
    "InvitationCreate",
    "InvitationResponse",
]
