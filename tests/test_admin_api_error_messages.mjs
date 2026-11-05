/**
 * Run: node tests/test_admin_api_error_messages.mjs
 *
 * Behavioral test for the admin.html fetch helper (api()):
 * non-JSON error responses must surface a clear Chinese HTTP-status hint,
 * never raw HTML source; JSON detail/message/error handling and success
 * passthrough must stay unchanged. No external dependencies.
 */
import { readFileSync } from "node:fs";
import assert from "node:assert";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "admin.html"), "utf8");

const start = html.indexOf("async function api(path, opts){");
const end = html.indexOf("\nfunction esc(", start);
assert.notEqual(start, -1, "api() function not found in admin.html");
assert.notEqual(end, -1, "api() function end marker missing");

// Extract the api source including its final closing brace.
let body = html.slice(start, end);
const lastBrace = body.lastIndexOf("}");
assert.notEqual(lastBrace, -1);
body = body.slice(0, lastBrace + 1);

function makeApi({ status, text }) {
  const ctx = {
    KEY: "test-key",
    appUrl: path => path, // This suite checks API errors at the root deployment.
    setAuth: () => {},
    fetch: async () => ({ status, ok: status >= 200 && status < 300, text: async () => text }),
  };
  vm.createContext(ctx);
  return vm.runInContext(body + "\napi", ctx, { filename: "admin-api.js" });
}

const run = async (name, fn) => {
  await fn();
  console.log("PASS", name);
};

const cloudflareHtml =
  "<!DOCTYPE html><html><head><title>502 Bad Gateway</title></head><body>" +
  "<center>cloudflare</center> lots of markup ... aa</body></html>";

await run("502 Cloudflare HTML -> neutral Chinese hint, no HTML leak, no nginx claim", async () => {
  const api = makeApi({ status: 502, text: cloudflareHtml });
  await assert.rejects(() => api("/admin/accounts/1/quota", { method: "POST" }), (e) => {
    const m = e.message;
    assert.match(m, /服务返回网关错误 \(HTTP 502\)/);
    assert.ok(!m.includes("<"), "must not leak HTML source");
    assert.ok(!/Nginx|nginx/i.test(m), "must not hard-code Nginx");
    return true;
  });
});

await run("502 nginx HTML -> same neutral 502 hint, no timeout claim", async () => {
  const api = makeApi({ status: 502, text: "<html><h1>502 Bad Gateway</h1></html>" });
  await assert.rejects(() => api("/admin/accounts"), (e) => {
    assert.match(e.message, /服务返回网关错误 \(HTTP 502\)/);
    assert.ok(!/timeout|超时/.test(e.message), "must not claim Nginx timeout for every 502");
    return true;
  });
});

for (const [status, snippet] of [
  [503, "服务暂时不可用"],
  [504, "网关超时"],
  [500, "服务端内部错误"],
  [429, "请求过于频繁"],
]) {
  await run(status + " non-JSON -> status hint", async () => {
    const api = makeApi({ status, text: "<html></html>" });
    await assert.rejects(() => api("/admin/accounts"), (e) => {
      assert.ok(e.message.includes(snippet));
      assert.ok(e.message.includes("HTTP " + status));
      return true;
    });
  });
}

await run("JSON detail preserved", async () => {
  const api = makeApi({ status: 409, text: '{"detail":"账号额度查询失败: session expired"}' });
  await assert.rejects(() => api("/admin/accounts/1/quota"), (e) => {
    assert.strictEqual(e.message, "账号额度查询失败: session expired");
    return true;
  });
});

await run("JSON error.message and error object still handled", async () => {
  const api1 = makeApi({ status: 400, text: '{"message":"m2a_key required"}' });
  await assert.rejects(() => api1("/x"), (e) => e.message === "m2a_key required");
  const api2 = makeApi({ status: 400, text: '{"error":{"message":"bad input"}}' });
  await assert.rejects(() => api2("/x"), (e) => e.message === "bad input");
});

await run("401 branch unchanged", async () => {
  const api = makeApi({ status: 401, text: '{"detail":"Unauthorized"}' });
  await assert.rejects(() => api("/admin/accounts"), (e) => e.message === "API Key 未填写或填写错误");
});

await run("success response passthrough unchanged", async () => {
  const payload = JSON.stringify({ ok: true, quota: 42 });
  const api = makeApi({ status: 200, text: payload });
  const data = await api("/admin/accounts/1/quota", { method: "POST" });
  assert.strictEqual(JSON.stringify(data), payload);
});

console.log("ALL PASS");
