# Publive CDS MCP Server

An MCP (Model Context Protocol) server that exposes all **Publive Content Delivery Service (CDS)** APIs as tools — letting any MCP-compatible AI (like Claude) query your Publive content directly.

---

## 🛠 Prerequisites

- **Node.js** v18 or later
- Publive API credentials (API Key, API Secret, Publisher ID)

---

## 📦 Installation

```bash
git clone <your-repo>
cd publive-cds-mcp
npm install
```

---

## ⚙️ Configuration

Set the following environment variables before running:

| Variable                | Description                        |
|-------------------------|------------------------------------|
| `PUBLIVE_API_KEY`        | Your Publive API Key               |
| `PUBLIVE_API_SECRET`     | Your Publive API Secret            |
| `PUBLIVE_PUBLISHER_ID`   | Your numeric Publisher ID          |

---

## 🚀 Running the Server

```bash
PUBLIVE_API_KEY=your_key \
PUBLIVE_API_SECRET=your_secret \
PUBLIVE_PUBLISHER_ID=your_publisher_id \
node index.js
```

---

## 🔌 Connect to Claude Desktop

Add this to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on Mac):

```json
{
  "mcpServers": {
    "publive-cds": {
      "command": "node",
      "args": ["/absolute/path/to/publive-cds-mcp/index.js"],
      "env": {
        "PUBLIVE_API_KEY": "your_key",
        "PUBLIVE_API_SECRET": "your_secret",
        "PUBLIVE_PUBLISHER_ID": "your_publisher_id"
      }
    }
  }
}
```

Restart Claude Desktop — the tools will appear automatically.

---

## 🧰 Available Tools

### Posts
| Tool | Description |
|------|-------------|
| `list_posts` | List & filter posts (by type, category, tag, author, date, title) |
| `get_post` | Get a single post by ID or slug |
| `get_post_by_url` | Get a post by its legacy/relative URL |

### Categories
| Tool | Description |
|------|-------------|
| `list_categories` | List all categories |
| `get_category` | Get a single category by ID or slug |

### Tags
| Tool | Description |
|------|-------------|
| `list_tags` | List all tags |
| `get_tag` | Get a single tag by ID or slug |

### Authors
| Tool | Description |
|------|-------------|
| `list_authors` | List all authors |
| `get_author` | Get a single author by ID or slug |

### Site & Utilities
| Tool | Description |
|------|-------------|
| `get_publisher_data` | Publisher branding, logo, social links |
| `identify_content` | Resolve a URL to its content type |
| `get_live_blog_updates` | Get live updates for a LiveBlog post |

---

## 🔍 Example Prompts (in Claude)

Once connected, you can ask Claude things like:

- *"List the latest 5 articles from Publive"*
- *"Get the post with slug union-budget-2026-highlights"*
- *"List all categories"*
- *"What type of content is at /news/some-article-12345?"*
- *"Get live blog updates for post ID 9876"*
- *"Show me all posts by author ID 3"*

---

## 🔒 Security Notes

- Never commit API credentials — always use environment variables
- The CDS API is **read-only** — this MCP server cannot modify any content
- Rotate your API keys periodically from the Publive Dashboard
