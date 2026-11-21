// Garmin MCP connector: a Cloudflare Worker that claude.ai connects to.
//   /mcp        MCP endpoint (Streamable HTTP, stateless). Needs an OAuth token.
//   /authorize  password-protected sign-in and consent page.
//   /token, /register, /.well-known/*  served by the OAuth library.

import OAuthProvider from "@cloudflare/workers-oauth-provider";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import { SCOPE, handleAuthorize, passwordVersion, type AuthEnv } from "./auth";
import { buildServer, type ToolEnv } from "./tools";

export interface Env extends ToolEnv, Omit<AuthEnv, "OAUTH_PROVIDER"> {
  /** Optional canonical origin, e.g. https://garmin-mcp.example.workers.dev. Defaults to the request's origin. */
  PUBLIC_URL?: string;
}

const mcpHandler = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext & { props?: { pwv?: string } }): Promise<Response> {
    // Tokens issued before the password last changed are no longer accepted.
    if (!env.CONNECTOR_PASSWORD || ctx.props?.pwv !== (await passwordVersion(env.CONNECTOR_PASSWORD))) {
      const origin = (env.PUBLIC_URL || new URL(request.url).origin).replace(/\/+$/, "");
      return new Response(JSON.stringify({ error: "invalid_token", error_description: "Signed out: connect again." }), {
        status: 401,
        headers: {
          "Content-Type": "application/json",
          "WWW-Authenticate": `Bearer error="invalid_token", resource_metadata="${origin}/.well-known/oauth-protected-resource/mcp"`,
        },
      });
    }
    if (request.method !== "POST") {
      // Stateless server: no server-to-client stream and no sessions to delete.
      return new Response(null, { status: 405, headers: { Allow: "POST" } });
    }
    const server = buildServer(env);
    const transport = new WebStandardStreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
    await server.connect(transport);
    return transport.handleRequest(request);
  },
};

const defaultHandler = {
  async fetch(request: Request, env: Env & { OAUTH_PROVIDER: AuthEnv["OAUTH_PROVIDER"] }): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/authorize") return handleAuthorize(request, env);
    if (url.pathname === "/" && request.method === "GET") {
      return new Response("Garmin MCP connector. Add " + url.origin + "/mcp as a custom connector in Claude.\n", {
        headers: { "Content-Type": "text/plain; charset=utf-8", "X-Robots-Tag": "noindex" },
      });
    }
    return new Response("Not found", { status: 404 });
  },
};

const DAY = 86400;
const providers = new Map<string, OAuthProvider<Env>>();

function providerFor(origin: string): OAuthProvider<Env> {
  let p = providers.get(origin);
  if (!p) {
    p = new OAuthProvider<Env>({
      apiRoute: "/mcp",
      apiHandler: mcpHandler as any,
      defaultHandler: defaultHandler as any,
      authorizeEndpoint: "/authorize",
      tokenEndpoint: "/token",
      clientRegistrationEndpoint: "/register",
      clientIdMetadataDocumentEnabled: true,
      scopesSupported: [SCOPE],
      requiredScopes: [SCOPE],
      resourceMetadata: { resource: origin + "/mcp", authorization_servers: [origin], resource_name: "Garmin" },
      accessTokenTTL: 3600,
      refreshTokenTTL: 30 * DAY,
      refreshTokenIdleTTL: 30 * DAY, // stays signed in while you keep using it
    });
    providers.set(origin, p);
  }
  return p;
}

export default {
  fetch(request: Request, env: Env, ctx: ExecutionContext) {
    const origin = (env.PUBLIC_URL || new URL(request.url).origin).replace(/\/+$/, "");
    return providerFor(origin).fetch(request, env, ctx);
  },
} satisfies ExportedHandler<Env>;
