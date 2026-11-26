/** Pinned DSH tools.guard seam; governed MCP calls remain subject to DiOS E2AG. */
export const inject = ['tools'];

export function apply(ctx) {
  // The upstream monotonic guard runs after policy/approval, before the tool body.
  // This composition exposes only the DiOS MCP namespace; dynamic plugin loading
  // and alternative executors must remain disabled in the admitted profile.
  ctx.tools.guard((execution) => {
    if (!/^mcp__dios__[A-Za-z][A-Za-z0-9_]*$/.test(execution.name)) {
      return 'DIOS_NATIVE_TOOL_DENIED: use an authorized DiOS MCP capability';
    }
  });
  // ACP declares this service as a required injection; a missing guard stays closed.
  ctx.provide('diosNativeGuard', true);
}
