"""CMS newsletter tools — subscribe/unsubscribe readers and verify subscriber emails."""
from ..clients.cms import cms_get, cms_post

SCHEMAS = [
    {
        "name": "subscribe_newsletter",
        "description": (
            "Subscribe an email address to one or more of the publisher's newsletter groups, "
            "optionally with a display name. "
            "NOTE: reCAPTCHA verification (which lets a subscriber skip the email-confirmation "
            "step) requires a browser widget token and is not supported via MCP — subscribers "
            "added through this tool go through the normal email verification flow "
            "(see verify_newsletter_subscriber_email)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["email"],
            "properties": {
                "email":     {"type": "string", "description": "Subscriber's email address"},
                "name":      {"type": "string", "description": "Subscriber's name (optional)"},
                "group_ids": {"type": "string", "description": "Newsletter group IDs to assign the subscriber to, as a comma-separated string (e.g. \"1,2,3\"). Omitted if empty."},
            },
        },
    },
    {
        "name": "unsubscribe_newsletter",
        "description": (
            "Unsubscribe an email address from the publisher's newsletter using the "
            "unsubscribe token and campaign identifier from the unsubscribe link in a "
            "campaign email."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["email"],
            "properties": {
                "email":             {"type": "string", "description": "Subscriber's email address"},
                "unsubscribe_token": {"type": "string", "description": "Token from the unsubscribe link in the campaign email (optional)"},
                "campaign":          {"type": "string", "description": "Campaign identifier from the unsubscribe link (optional)"},
            },
        },
    },
    {
        "name": "verify_newsletter_subscriber_email",
        "description": (
            "Confirm a newsletter subscriber's email address using the email and token from "
            "the verification email sent after subscribe_newsletter. Both fields are required."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["email", "token"],
            "properties": {
                "email": {"type": "string", "description": "Subscriber's email address"},
                "token": {"type": "string", "description": "Verification token from the email link"},
            },
        },
    },
]


def subscribe_newsletter(credentials: dict, args: dict):
    payload = {"email": args["email"], "name": args.get("name", "")}
    group_ids = args.get("group_ids")
    if group_ids:
        payload["group_ids"] = group_ids
    return cms_post(credentials, "/newsletter/subscribe/", payload)


def unsubscribe_newsletter(credentials: dict, args: dict):
    payload = {
        "email":             args["email"],
        "unsubscribe_token": args.get("unsubscribe_token", ""),
        "campaign":          args.get("campaign", ""),
    }
    return cms_post(credentials, "/newsletter/unsubscribe/", payload)


def verify_newsletter_subscriber_email(credentials: dict, args: dict):
    return cms_get(credentials, "/newsletter/verify-email/", {
        "email": args["email"],
        "token": args["token"],
    })


HANDLERS = {
    "subscribe_newsletter":               subscribe_newsletter,
    "unsubscribe_newsletter":             unsubscribe_newsletter,
    "verify_newsletter_subscriber_email": verify_newsletter_subscriber_email,
}
