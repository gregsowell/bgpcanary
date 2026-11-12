# The MCP server: ask an AI assistant about your network

bgpcanary has a built-in [MCP](https://modelcontextprotocol.io/) server, so an AI assistant that speaks MCP
(Claude Code, Cursor, VS Code, Windsurf, Claude Desktop through a bridge, and many others) can read your
bgpcanary and answer questions like:

- "Is anything wrong with my network right now?"
- "What happened to 192.0.2.0/24 last night?"
- "Summarize the open events and who I should call."
- "What is AS64499 to me? Should I file it as a peer?"
- "Write the ticket to my upstream for event 412."

It is part of bgpcanary and runs on your server, at `/mcp`. It is **off until an admin turns it on**, and it is
read-only: every user connects with their own [API token](API.md#tokens), and the assistant can look but never
change anything.

## Turning it on

**Settings › MCP server**, tick *Serve the MCP endpoint*, save. Off, the address answers 404 to everyone. On, it
answers only requests that carry a valid token; a browser's sign-in doesn't count there, on purpose.

Nothing else changes on the server: it is the same process, behind the same nginx or reverse proxy, with no new
dependency. If your proxy limits request bodies, 256 KB is more than enough.

## Connecting an assistant

1. Create a token under **Settings › API tokens** (any account; a viewer's is enough) and copy it.
2. Give the assistant the address, `https://bgp.example.com/mcp`, and the token in an `Authorization` header.

### Claude Code

```bash
claude mcp add --transport http bgpcanary https://bgp.example.com/mcp --header "Authorization: Bearer bgpc_..."
```

`claude mcp list` should then show it connected. Ask away.

### Cursor, VS Code, Windsurf and others with a JSON config

```json
{
  "mcpServers": {
    "bgpcanary": {
      "url": "https://bgp.example.com/mcp",
      "headers": { "Authorization": "Bearer bgpc_..." }
    }
  }
}
```

VS Code's file (`.vscode/mcp.json` or the user settings) names the entry under `servers` with `"type": "http"`;
the `url` and `headers` lines are the same.

### Claude Desktop

Claude Desktop only starts local servers itself, so it needs a small bridge to reach a remote one:
[mcp-remote](https://github.com/geelen/mcp-remote), which needs Node.js. In its config file (Settings ›
Developer › Edit Config):

```json
{
  "mcpServers": {
    "bgpcanary": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://bgp.example.com/mcp", "--header", "Authorization:${AUTH}"],
      "env": { "AUTH": "Bearer bgpc_..." }
    }
  }
}
```

(The token goes through an environment variable because Claude Desktop mangles spaces in arguments.) Restart
Claude Desktop; the bgpcanary tools appear in the tools menu.

### Anything else

Any MCP client that speaks the Streamable HTTP transport and can send a header works the same way. Clients that
insist on OAuth (claude.ai's remote connectors, for one) can't connect yet: bgpcanary doesn't run an OAuth server.

## What the assistant can do

| Tool | What it returns |
|---|---|
| `bgpcanary_status` | The dashboard's table: each prefix's state, visibility against its baseline, the neighbours its paths come in through, missing upstreams, and how many events are open. |
| `bgpcanary_events` | The events, with the Events page's filters: open or closed, kind, severity, prefix, the last N hours or a since/until window. |
| `bgpcanary_event` | One event with the alerts sent for it, and on request the plain-text evidence report for a provider ticket, which opens with what else went on around the event. |
| `bgpcanary_neighbour` | What a network is to you: transit, peer, exchange peer, or unfiled; the paths through it now; the events about it. |
| `bgpcanary_whoami` | Which bgpcanary it talks to and as whom. Ask for it when something doesn't work. |

Every tool is marked read-only for the assistant, and the server tells it what the fields mean (visibility,
baseline, the states, the event kinds), so it answers in plain language. Times come as unix seconds with ISO 8601
copies. The tools return the same data as the [API](API.md), and their fields are part of the same promise: only
ever added.

## Security

- **Off by default.** An admin turns it on; the Settings card shows whether it's on.
- **A token, never a cookie.** The endpoint accepts only an API token; the browser's session is refused there, so
  a page you open can't drive it. Bad tokens are rate-limited per address.
- **Read-only.** A token can read what its owner can, and the server exposes nothing but reads. The assistant
  can't change settings, file a neighbour, or start checks.
- **The token is a password.** It sits in the assistant's config file; protect that file as you would any
  credential, and never put the token in a URL. Revoke it under Settings › API tokens if it leaks.
- **What the assistant reads is data from the internet**: AS names, event titles. Treat its summaries as you
  would any summary of untrusted text.
- Each tool call is logged on the server with the account and the token's name.

## Troubleshooting

- **404 "the MCP server is off"**: turn it on under Settings › MCP server.
- **401 "login required"**: no token, a mistyped one, or a revoked one. Create a new one.
- **403 "password change required"**: the token's account must change its password in bgpcanary first.
- **The assistant doesn't list the tools**: restart it after editing its config, and check its MCP log. In
  Claude Code, `claude mcp list` says whether it connected.
- **Certificate errors**: the assistant's machine must trust your site's certificate; with a private CA, install
  the CA there.
- **Test it by hand** with curl (the first call every client makes):

```bash
curl -s https://bgp.example.com/mcp -H "Authorization: Bearer bgpc_..." -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

It answers with the server's name, version and instructions. `"method":"tools/list"` lists the tools.
