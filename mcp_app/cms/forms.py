"""CMS form submission tool."""
from ..clients.cms import cms_post

SCHEMAS = [
    {
        "name": "submit_form",
        "description": (
            "Submit a Publive form. Requires a reCAPTCHA token from the client. "
            "Dynamic fields are defined by the form schema — fetch the schema first with "
            "fetch_form_schema to discover required field names and types. "
            "Note: file upload fields are not supported via MCP; use the dashboard or direct API for forms with file attachments."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["form_schema_id", "recaptcha_token"],
            "properties": {
                "form_schema_id":   {"type": "string", "description": "Form schema ID (24-character hex, same as schema_id in fetch_form_schema)"},
                "recaptcha_token":  {"type": "string", "description": "reCAPTCHA token from the client-side widget"},
                "fields":           {"type": "object", "description": "Dynamic field values as a key→value object matching the schema's field slugs (e.g. {\"name\": \"Jane\", \"email\": \"jane@example.com\"})"},
            },
        },
    },
]


def submit_form(credentials: dict, args: dict):
    form_schema_id  = args["form_schema_id"]
    recaptcha_token = args["recaptcha_token"]
    dynamic_fields  = args.get("fields", {})
    payload = {
        "schema":              form_schema_id,
        "g-recaptcha-response": recaptcha_token,
        **dynamic_fields,
    }
    return cms_post(credentials, f"/form/{form_schema_id}/submit/", payload)


HANDLERS = {
    "submit_form": submit_form,
}
