import {
  ApiError,
  errorMessage,
  isPending,
  request,
  requestId,
  safeJobUrl,
  setCsrfToken,
  taskLabel,
} from "../src/api";

it("sends same-origin JSON mutations with the current CSRF token", async () => {
  const fetcher = vi
    .fn()
    .mockImplementation(async () => new Response('{"ok":true}'));
  vi.stubGlobal("fetch", fetcher);
  setCsrfToken("csrf");
  expect(
    await request("/jobs/1/state", {
      method: "PATCH",
      body: { applied: true },
    }),
  ).toEqual({ ok: true });
  expect(fetcher).toHaveBeenCalledWith(
    "/api/jobs/1/state",
    expect.objectContaining({
      credentials: "same-origin",
      body: '{"applied":true}',
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": "csrf",
      },
    }),
  );
  setCsrfToken();
  await request("/read", { method: "GET" });
  expect(fetcher.mock.calls[1][1].headers).toEqual({
    Accept: "application/json",
  });
});

it("lets the browser set multipart boundaries and supports empty responses", async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetcher);
  const body = new FormData();
  body.append("file", new File(["data"], "CV.pdf"));
  expect(await request("/upload", { method: "POST", body })).toBeUndefined();
  expect(fetcher.mock.calls[0][1].body).toBe(body);
  expect(fetcher.mock.calls[0][1].headers["Content-Type"]).toBeUndefined();
});

it("surfaces server errors, malformed errors and expired authorization", async () => {
  const expired = vi.fn();
  window.addEventListener("session-expired", expired);
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValueOnce(
        new Response('{"detail":"Expired"}', { status: 401 }),
      )
      .mockResolvedValueOnce(new Response('{"detail":[]}', { status: 422 }))
      .mockResolvedValueOnce(new Response("gateway down", { status: 502 })),
  );
  await expect(request("/private")).rejects.toMatchObject({
    status: 401,
    message: "Expired",
  });
  expect(expired).toHaveBeenCalledOnce();
  await expect(request("/bad")).rejects.toBeInstanceOf(ApiError);
  await expect(request("/broken")).rejects.toThrow("Запрос не выполнен");
  window.removeEventListener("session-expired", expired);
  expect(errorMessage(new Error("offline"))).toBe("offline");
  expect(errorMessage(null)).toContain("сервером");
});

it.each([
  "javascript:alert(1)",
  "file:///tmp/CV.pdf",
  "https://a:b@example.com",
  "https://user@example.com",
  "",
  "/relative",
  "data:text/html,x",
])("rejects unsafe vacancy URL %s", (value) => {
  expect(safeJobUrl(value)).toBeNull();
});
it("accepts absolute HTTP links and returns stable task descriptions", () => {
  expect(safeJobUrl("https://example.com/jobs")).toBe(
    "https://example.com/jobs",
  );
  expect(safeJobUrl("http://example.com")).toBe("http://example.com/");
  expect(taskLabel("ready")).toBe("Готово");
  expect(taskLabel("unknown")).toBe("unknown");
  expect(isPending("verifying")).toBe(true);
  expect(isPending("blocked")).toBe(false);
  expect(requestId()).not.toBe(requestId());
});
