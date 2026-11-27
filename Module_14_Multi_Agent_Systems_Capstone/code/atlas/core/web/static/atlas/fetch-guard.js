// Every write the dashboard makes carries X-Atlas-Client. The server refuses
// API writes without it (core/web/guard.py): a page on another origin cannot
// add a custom header without a CORS preflight, which Atlas never grants.
(() => {
  const plain = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
      const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
      headers.set("X-Atlas-Client", "1");
      init = { ...init, headers };
    }
    return plain(input, init);
  };
})();
