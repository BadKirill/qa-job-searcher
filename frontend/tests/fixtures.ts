import type { Job, Profile, Session, Task, Template } from "../src/types";

export const template: Template = {
  id: "template-1",
  filename: "Candidate.docx",
  status: "draft",
  facts: [
    {
      id: "fact-1",
      text: "Five years of API testing experience.",
      source: "paragraph 1",
    },
  ],
  instructions: "",
  created_at: "2026-10-03",
  extracted_text: "Five years of API testing experience.",
};

export function makeJob(id = "job-1", overrides: Partial<Job> = {}): Job {
  return {
    id,
    title: `Senior QA Engineer ${id}`,
    company: "Acme",
    location: "Europe · Remote",
    url: "https://example.com/jobs/qa",
    description: "Build reliable products.",
    salary: "€ 4,000 – 5,000 / month",
    score: 12,
    published_at: "2026-10-02",
    applied_at: null,
    archived_at: null,
    documents: [],
    details: {
      qa_types: ["Manual", "Auto"],
      languages: ["English B2"],
      programming_languages: ["Python"],
      responsibilities: ["Test APIs", "Write automated checks"],
      requirements: ["API testing experience"],
    },
    ...overrides,
  };
}

export function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    kind: "cv",
    status: "queued",
    artifacts: [],
    job_id: "job-1",
    created_at: "2026-10-03",
    ...overrides,
  };
}

export class TestEvents {
  static latest: TestEvents;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: (() => void) | null = null;
  change: (() => void) | null = null;
  close = vi.fn();
  constructor() {
    TestEvents.latest = this;
  }
  addEventListener(_name: string, listener: () => void) {
    this.change = listener;
  }
}

export function server() {
  const state: {
    session: Session;
    profile: Profile;
    jobs: Job[];
    tasks: Task[];
    messages: {
      id: string;
      role: string;
      text: string;
      status?: string;
      created_at: string;
    }[];
  } = {
    session: {
      authenticated: true,
      setup_required: false,
      csrf_token: "test-csrf",
    },
    profile: {
      active: { ...template, status: "active", version: "1" },
      drafts: [],
    },
    jobs: Array.from({ length: 11 }, (_, index) => makeJob(`job-${index + 1}`)),
    tasks: [],
    messages: [],
  };
  const failures: { path: string; status: number; detail: string }[] = [];
  const calls: {
    path: string;
    method: string;
    body: unknown;
    headers: HeadersInit | undefined;
  }[] = [];
  const fetcher = vi.fn(async (url: string, init: RequestInit = {}) => {
    const parsed = new URL(url, "http://localhost");
    const path = parsed.pathname.replace("/api", "");
    const method = init.method ?? "GET";
    const body =
      typeof init.body === "string" ? JSON.parse(init.body) : init.body;
    calls.push({ path, method, body, headers: init.headers });
    const failed = failures.findIndex((item) => item.path === path);
    if (failed >= 0) {
      const failure = failures.splice(failed, 1)[0];
      return new Response(JSON.stringify({ detail: failure.detail }), {
        status: failure.status,
      });
    }
    let data: unknown;
    if (path === "/session") data = state.session;
    else if (path === "/setup" || path === "/login") {
      state.session = {
        authenticated: true,
        setup_required: false,
        csrf_token: "test-csrf",
      };
      data = state.session;
    } else if (path === "/logout") return new Response(null, { status: 204 });
    else if (path === "/profile") data = state.profile;
    else if (path === "/tasks") data = { items: state.tasks };
    else if (path === "/jobs") {
      const page = Number(parsed.searchParams.get("page"));
      const archived = parsed.searchParams.get("tab") === "archive";
      const jobs = state.jobs.filter((job) => !!job.archived_at === archived);
      data = {
        items: jobs.slice((page - 1) * 10, page * 10),
        total: jobs.length,
        page,
        pages: Math.ceil(jobs.length / 10),
        snapshot_id: "snapshot-1",
      };
    } else if (path.endsWith("/state")) {
      const job = state.jobs.find((item) => item.id === path.split("/")[2])!;
      if ("archived" in body)
        job.archived_at = body.archived ? "2026-10-03" : null;
      if ("applied" in body)
        job.applied_at = body.applied ? "2026-10-03" : null;
      data = job;
    } else if (path.endsWith("/documents")) {
      const task = makeTask({
        id: `task-${state.tasks.length + 1}`,
        kind: body.kind,
      });
      state.tasks.push(task);
      data = task;
    } else if (path === "/search-runs") {
      const task = makeTask({ kind: "search" });
      state.tasks.push(task);
      data = task;
    } else if (path === "/template-drafts") {
      const draft = {
        ...structuredClone(template),
        filename: (body.get("file") as File).name,
      };
      state.profile.drafts.push(draft);
      data = draft;
    } else if (path.endsWith("/activate")) {
      const draft = state.profile.drafts.find(
        (item) => item.id === path.split("/")[2],
      )!;
      state.profile.active = { ...draft, status: "active", version: "2" };
      state.profile.drafts = [];
      data = state.profile.active;
    } else if (path.startsWith("/template-drafts/")) {
      const draft = state.profile.drafts.find(
        (item) => item.id === path.split("/")[2],
      )!;
      Object.assign(draft, body);
      data = draft;
    } else if (path === "/conversations") data = { id: "conversation-1" };
    else if (path.endsWith("/messages")) {
      if (method === "POST")
        state.messages.push({
          id: String(state.messages.length),
          role: "user",
          text: body.text,
          status: "queued",
          created_at: "2026-10-03",
        });
      data = { items: state.messages };
    } else throw new Error(`Unhandled ${method} ${path}`);
    return new Response(JSON.stringify(data), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetcher);
  vi.stubGlobal("EventSource", TestEvents);
  return { state, calls, failures, fetcher };
}
