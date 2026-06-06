"""Shared helpers for all CMS write tools: preview formatters and write guards."""
from mcp_app.clients.cms import cms_get

# Returned by every delete handler when the caller hasn't passed both
# dry_run=false and confirm_delete=true.
DELETION_REQUIRES_CONFIRMATION: dict = {
    "error_type": "confirmation_required",
    "message": (
        "Deletion requires BOTH dry_run=false AND confirm_delete=true. "
        "Call again with both parameters set to confirm you want to permanently delete this resource."
    ),
    "retryable": False,
}


def format_field_value(v) -> str:
    """Truncate long values for human-readable diff output."""
    if v is None:
        return "(empty)"
    s = str(v)
    return s[:120] + "…" if len(s) > 120 else s


def preview_create_op(resource: str, payload: dict) -> str:
    lines = [
        f"📋  DRY RUN — Create {resource}",
        "─" * 52,
        f"Will create a new {resource.lower()} with the following details:",
        "",
    ]
    for k, v in payload.items():
        lines.append(f"  {k:<28} {format_field_value(v)}")
    lines += [
        "",
        "⚡  No changes have been made.",
        "To proceed, call this tool again with dry_run=false.",
    ]
    return "\n".join(lines)


def preview_update_op(resource: str, item_id, current: dict, changes: dict) -> str:
    lines = [
        f"📋  DRY RUN — Update {resource} #{item_id}",
        "─" * 52,
        "The following fields will change:",
        "",
    ]
    has_diff = False
    for field, new_val in changes.items():
        old_val = current.get(field)
        lines.append(f"  {field:<28} {format_field_value(old_val)}  →  {format_field_value(new_val)}")
        has_diff = True
    if not has_diff:
        lines.append("  (no fields provided — nothing will change)")
    lines += ["", "⚡  No changes have been made.", "To apply, call again with dry_run=false."]
    return "\n".join(lines)


def preview_delete_op(resource: str, item_id, item: dict, warning: str = "") -> str:
    lines = [
        f"📋  DRY RUN — Delete {resource} #{item_id}",
        "─" * 52,
        f"⚠️   WARNING: This will PERMANENTLY delete the following {resource.lower()}:",
        "",
    ]
    for k, v in item.items():
        lines.append(f"  {k:<28} {format_field_value(v)}")
    if warning:
        lines += ["", f"⚠️   {warning}"]
    lines += [
        "",
        "⚡  No changes have been made.",
        "To permanently delete, call again with:",
        "  dry_run=false",
        "  confirm_delete=true",
    ]
    return "\n".join(lines)


def validate_live_blog_post_type(credentials: dict, post_id: int):
    """Return an error dict if post_id doesn't exist or isn't a LiveBlog; else None."""
    post = cms_get(credentials, f"/post/{post_id}/")
    if "error_type" in post:
        if post.get("error_type") == "not_found":
            return {
                "error_type": "not_found",
                "message": (
                    f"Post {post_id} was not found in the CMS. "
                    "Check that the post ID is correct and that the post exists."
                ),
                "retryable": False,
            }
        return post
    if post.get("type") != "LiveBlog":
        return {
            "error_type": "bad_request",
            "message": (
                f"Post {post_id} is a '{post.get('type')}' post, not a LiveBlog. "
                "Live blog updates can only be added to LiveBlog posts."
            ),
            "retryable": False,
        }
    return None
