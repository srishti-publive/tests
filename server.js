/**
 * Publive CDS — Remote MCP Server
 *
 * Flow:
 *  1. User adds the hosted URL to Claude Desktop config (no credentials needed in config)
 *  2. Claude opens /connect → user sees the auth webpage
 *  3. User enters Publisher ID, API Key, API Secret → server validates against Publive CDS
 *  4. On success → session is created, MCP tools become available
 *  5. Claude can now call all CDS tools authenticated as that user
 */

import express from "express";
import session from "express-session";
import cors from "cors";
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { SSEServerTransport } from "@modelcontextprotocol/sdk/server/sse.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const app = express();
const PORT = process.env.PORT || 3000;
const SESSION_SECRET = process.env.SESSION_SECRET || "change-me-in-production";
const BASE_URL = process.env.BASE_URL || `http://localhost:${PORT}`;

// ─── Middleware ───────────────────────────────────────────────────────────────

app.use(cors({ origin: true, credentials: true }));
app.use(express.json());
app.use(express.urlencoded({ extended: true }));
app.use(
  session({
    secret: SESSION_SECRET,
    resave: false,
    saveUninitialized: false,
    cookie: { secure: false, httpOnly: true, maxAge: 24 * 60 * 60 * 1000 },
  })
);
app.use(express.static(join(__dirname, "public")));

// ─── Active MCP transports (keyed by sessionId) ───────────────────────────────
const transports = {};

// ─── Publive CDS API Helper ───────────────────────────────────────────────────

async function cdsGet(credentials, path, params = {}) {
  const { publisherId, apiKey, apiSecret } = credentials;
  const token = Buffer.from(`${apiKey}:${apiSecret}`).toString("base64");
  const url = new URL(
    `https://cds.thepublive.com/publisher/${publisherId}${path}`
  );
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  }
  const res = await fetch(url.toString(), {
    headers: { Authorization: `Basic ${token}` },
  });
  const json = await res.json();
  if (!res.ok) throw new Error(json?.detail || json?.message || `HTTP ${res.status}`);
  return json;
}

// ─── Auth Routes ──────────────────────────────────────────────────────────────

// Show the auth page (Claude Desktop opens this when credentials are needed)
app.get("/connect", (req, res) => {
  res.sendFile(join(__dirname, "public", "connect.html"));
});

// Handle credentials form submission
app.post("/auth/login", async (req, res) => {
  const { publisherId, apiKey, apiSecret } = req.body;

  if (!publisherId || !apiKey || !apiSecret) {
    return res.status(400).json({ error: "All fields are required." });
  }

  // Validate credentials by making a real CDS call
  try {
    await cdsGet({ publisherId, apiKey, apiSecret }, "/publisher-data/");
  } catch (err) {
    return res.status(401).json({
      error: "Invalid credentials. Please check your Publisher ID, API Key, and API Secret.",
    });
  }

  // Store in session
  req.session.credentials = { publisherId, apiKey, apiSecret };
  req.session.authenticatedAt = new Date().toISOString();

  return res.json({ success: true, redirectTo: "/auth/success" });
});

// Success page shown after auth
app.get("/auth/success", (req, res) => {
  if (!req.session.credentials) return res.redirect("/connect");
  res.sendFile(join(__dirname, "public", "success.html"));
});

// Logout
app.post("/auth/logout", (req, res) => {
  req.session.destroy();
  res.json({ success: true });
});

// Check auth status
app.get("/auth/status", (req, res) => {
  res.json({
    authenticated: !!req.session.credentials,
    publisherId: req.session.credentials?.publisherId,
  });
});

// ─── MCP Endpoint (SSE) ───────────────────────────────────────────────────────

app.get("/mcp", async (req, res) => {
  // If not authenticated, redirect to connect page
  if (!req.session?.credentials) {
    res.status(401).json({
      error: "Not authenticated",
      authUrl: `${BASE_URL}/connect`,
      message: "Please visit the authUrl to connect your Publive account first.",
    });
    return;
  }

  const credentials = req.session.credentials;
  const sessionId = req.session.id;

  // Create MCP server instance for this connection
  const server = createMCPServer(credentials);
  const transport = new SSEServerTransport("/mcp/message", res);
  transports[sessionId] = transport;

  res.on("close", () => {
    delete transports[sessionId];
  });

  await server.connect(transport);
});

app.post("/mcp/message", async (req, res) => {
  const sessionId = req.session?.id;
  const transport = transports[sessionId];
  if (!transport) {
    return res.status(400).json({ error: "No active MCP session. Visit /connect first." });
  }
  await transport.handlePostMessage(req, res);
});

// ─── MCP Server Factory ───────────────────────────────────────────────────────

function createMCPServer(credentials) {
  const server = new Server(
    { name: "publive-cds", version: "1.0.0" },
    { capabilities: { tools: {} } }
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    try {
      const result = await callTool(credentials, name, args || {});
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Error: ${err.message}` }],
        isError: true,
      };
    }
  });

  return server;
}

// ─── Tool Definitions ─────────────────────────────────────────────────────────

const TOOLS = [
  {
    name: "list_posts",
    description: "List and filter published posts. Supports filtering by type, category, tag, author, date range, title search, and pagination.",
    inputSchema: {
      type: "object",
      properties: {
        page:  { type: "integer", description: "Page number (default: 1, max: 1000)" },
        limit: { type: "integer", description: "Items per page (default: 10, max: 50)" },
        type__eq:                 { type: "string",  description: "Filter by type: Article, Video, Web Story, Gallery, LiveBlog" },
        type__in:                 { type: "string",  description: "Multiple types comma-separated e.g. Article,Video" },
        title__contains:          { type: "string",  description: "Search by title substring" },
        "categories.id__eq":      { type: "integer", description: "Filter by category ID" },
        "tags.id__eq":            { type: "integer", description: "Filter by tag ID" },
        "contributors.id__eq":    { type: "integer", description: "Filter by author ID" },
        "primary_category.id__eq":{ type: "integer", description: "Filter by primary category ID" },
        created_at__gte:          { type: "string",  description: "Posts created on or after (ISO 8601)" },
        created_at__lte:          { type: "string",  description: "Posts created on or before (ISO 8601)" },
        word_count__gt:           { type: "integer", description: "Word count greater than" },
        word_count__lt:           { type: "integer", description: "Word count less than" },
      },
    },
  },
  {
    name: "get_post",
    description: "Get full details of a single post by ID or slug.",
    inputSchema: {
      type: "object",
      required: ["identifier"],
      properties: {
        identifier: { type: "string", description: "Post ID or slug" },
      },
    },
  },
  {
    name: "get_post_by_url",
    description: "Get a post by its legacy or relative URL path.",
    inputSchema: {
      type: "object",
      required: ["legacy_url"],
      properties: {
        legacy_url: { type: "string", description: "Relative URL e.g. /business/article-slug-12345" },
      },
    },
  },
  {
    name: "list_categories",
    description: "List all categories with hierarchical structure.",
    inputSchema: {
      type: "object",
      properties: {
        page:  { type: "integer" },
        limit: { type: "integer" },
      },
    },
  },
  {
    name: "get_category",
    description: "Get a single category by ID or slug including SEO metadata and child categories.",
    inputSchema: {
      type: "object",
      required: ["identifier"],
      properties: { identifier: { type: "string", description: "Category ID or slug" } },
    },
  },
  {
    name: "list_tags",
    description: "List all tags.",
    inputSchema: {
      type: "object",
      properties: { page: { type: "integer" }, limit: { type: "integer" } },
    },
  },
  {
    name: "get_tag",
    description: "Get a single tag by ID or slug.",
    inputSchema: {
      type: "object",
      required: ["identifier"],
      properties: { identifier: { type: "string", description: "Tag ID or slug" } },
    },
  },
  {
    name: "list_authors",
    description: "List all authors with profile info and social links.",
    inputSchema: {
      type: "object",
      properties: { page: { type: "integer" }, limit: { type: "integer" } },
    },
  },
  {
    name: "get_author",
    description: "Get a single author by ID or slug.",
    inputSchema: {
      type: "object",
      required: ["identifier"],
      properties: { identifier: { type: "string", description: "Author ID or slug" } },
    },
  },
  {
    name: "get_publisher_data",
    description: "Get publisher profile: branding, logo, colors, social links, site metadata.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "identify_content",
    description: "Resolve a URL path to its content type: post, category, tag, author, redirect, or not_found.",
    inputSchema: {
      type: "object",
      required: ["legacy_url"],
      properties: {
        legacy_url: { type: "string", description: "Path to resolve e.g. /guides/getting-started" },
      },
    },
  },
  {
    name: "get_live_blog_updates",
    description: "Get live blog updates for a LiveBlog post.",
    inputSchema: {
      type: "object",
      required: ["post_id"],
      properties: {
        post_id: { type: "integer", description: "LiveBlog post ID" },
        page:    { type: "integer" },
        limit:   { type: "integer" },
      },
    },
  },
];

// ─── Tool Handlers ────────────────────────────────────────────────────────────

async function callTool(credentials, name, args) {
  switch (name) {
    case "list_posts": {
      const { page, limit, ...filters } = args;
      return await cdsGet(credentials, "/posts/", { page, limit, ...filters });
    }
    case "get_post":
      return await cdsGet(credentials, `/post/${args.identifier}/`);
    case "get_post_by_url":
      return await cdsGet(credentials, "/post/", { legacy_url: args.legacy_url });
    case "list_categories":
      return await cdsGet(credentials, "/categories/", { page: args.page, limit: args.limit });
    case "get_category":
      return await cdsGet(credentials, `/category/${args.identifier}/`);
    case "list_tags":
      return await cdsGet(credentials, "/tags/", { page: args.page, limit: args.limit });
    case "get_tag":
      return await cdsGet(credentials, `/tag/${args.identifier}/`);
    case "list_authors":
      return await cdsGet(credentials, "/contributors/", { page: args.page, limit: args.limit });
    case "get_author":
      return await cdsGet(credentials, `/contributor/${args.identifier}/`);
    case "get_publisher_data":
      return await cdsGet(credentials, "/publisher-data/");
    case "identify_content":
      return await cdsGet(credentials, "/identify_url/", { legacy_url: args.legacy_url });
    case "get_live_blog_updates":
      return await cdsGet(credentials, `/live-blog/${args.post_id}/updates/`, {
        page: args.page,
        limit: args.limit,
      });
    default:
      throw new Error(`Unknown tool: ${name}`);
  }
}

// ─── Start ────────────────────────────────────────────────────────────────────

app.listen(PORT, () => {
  console.log(`\n✅ Publive CDS MCP Server running`);
  console.log(`   Auth page : ${BASE_URL}/connect`);
  console.log(`   MCP endpoint: ${BASE_URL}/mcp`);
  console.log(`\n   Claude Desktop config:`);
  console.log(`   "url": "${BASE_URL}/mcp"\n`);
});