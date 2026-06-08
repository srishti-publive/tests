"""CMS reader account tools — login, registration, password reset, and email verification.

reader/logout is intentionally not implemented: per the docs it makes no backend call and
performs no token invalidation (token cleanup is fully client-side) — there's nothing for
an MCP tool to do.
"""
from ..clients.cms import cms_patch, cms_post

SCHEMAS = [
    {
        "name": "login_reader",
        "description": (
            "Authenticate a reader with email and password and return an opaque session token. "
            "NOTE: this only covers the email/password flow — Google/Facebook OAuth login "
            "(which requires a browser redirect) is not supported via MCP. If the publisher has "
            "reCAPTCHA configured for reader login, this call may be rejected since there is no "
            "MCP-compatible way to supply a reCAPTCHA token."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["email", "password"],
            "properties": {
                "email":    {"type": "string", "description": "Reader's email address (normalized to lowercase before forwarding)"},
                "password": {"type": "string", "description": "Reader's password"},
            },
        },
    },
    {
        "name": "register_reader",
        "description": (
            "Create a new reader account and trigger a verification email. The reader must "
            "verify their email (see verify_reader_email) before they can log in."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["email", "password"],
            "properties": {
                "email":    {"type": "string", "description": "Reader's email address (normalized to lowercase before forwarding)"},
                "password": {"type": "string", "description": "Reader's password"},
                "name":     {"type": "string", "description": "Reader's display name (optional)"},
                "picture":  {"type": "string", "description": "URL of the reader's profile picture (optional)"},
            },
        },
    },
    {
        "name": "forgot_password_reader",
        "description": (
            "Trigger a password-reset email for a reader. The email contains a one-time token "
            "to pass to reset_password_reader."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["email"],
            "properties": {
                "email": {"type": "string", "description": "Reader's email address (normalized to lowercase before forwarding)"},
            },
        },
    },
    {
        "name": "reset_password_reader",
        "description": (
            "Set a new password for a reader using the one-time reset token sent by "
            "forgot_password_reader. new_password and confirm_password must match — the "
            "request is rejected with an error if they don't."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["email", "token", "new_password", "confirm_password"],
            "properties": {
                "email":            {"type": "string", "description": "Reader's email address (normalized to lowercase before forwarding)"},
                "token":            {"type": "string", "description": "One-time reset token from the password reset email"},
                "new_password":     {"type": "string", "description": "The reader's new password"},
                "confirm_password": {"type": "string", "description": "Must match new_password"},
            },
        },
    },
    {
        "name": "verify_reader_email",
        "description": (
            "Verify a reader's email address using the email and token from the verification "
            "email sent after register_reader."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["email", "token"],
            "properties": {
                "email": {"type": "string", "description": "Reader's email address (normalized to lowercase before forwarding)"},
                "token": {"type": "string", "description": "Verification token from the email link"},
            },
        },
    },
]


def login_reader(credentials: dict, args: dict):
    return cms_post(credentials, "/reader/login/", {
        "email":    args["email"].lower(),
        "password": args["password"],
    })


def register_reader(credentials: dict, args: dict):
    return cms_post(credentials, "/reader/register/", {
        "email":    args["email"].lower(),
        "password": args["password"],
        "name":     args.get("name", ""),
        "picture":  args.get("picture", ""),
    })


def forgot_password_reader(credentials: dict, args: dict):
    return cms_post(credentials, "/reader/forgot-password/", {"email": args["email"].lower()})


def reset_password_reader(credentials: dict, args: dict):
    return cms_patch(credentials, "/reader/reset-password/", {
        "email":            args["email"].lower(),
        "token":            args["token"],
        "new_password":     args["new_password"],
        "confirm_password": args["confirm_password"],
    })


def verify_reader_email(credentials: dict, args: dict):
    return cms_post(credentials, "/reader/verify-email/", {
        "email": args["email"].lower(),
        "token": args["token"],
    })


HANDLERS = {
    "login_reader":           login_reader,
    "register_reader":        register_reader,
    "forgot_password_reader": forgot_password_reader,
    "reset_password_reader":  reset_password_reader,
    "verify_reader_email":    verify_reader_email,
}
