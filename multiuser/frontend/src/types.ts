export type AccountUser = {
  id: string;
  login: string;
  display_name: string;
  role: "admin" | "user";
};
export type Session = {
  maintenance?: boolean;
  account_mode?: boolean;
  user?: AccountUser | null;
  authenticated: boolean;
  setup_required: boolean;
  csrf_token?: string;
};
export type Fact = {
  id: string;
  text: string;
  source: string;
  kind?: "body" | "bullet" | "entry" | "role" | "contact" | "heading" | "title";
};
export type Template = {
  id: string;
  filename: string;
  status: string;
  version?: string;
  facts: Fact[];
  instructions: string;
  created_at: string;
  extracted_text: string;
  conversation_id?: string;
  reference?: { sha256: string; page_count: number } | null;
};
export type Profile = { active: Template | null; drafts: Template[] };
export type Artifact = { id: string; format: string; filename: string };
export type Task = {
  id: string;
  kind: string;
  status: string;
  error?: string | null;
  job_id?: string;
  artifacts: Artifact[];
  created_at: string;
  conversation_id?: string;
  preview_available?: boolean;
  input_summary?: {
    template_id?: string;
    template_version?: string;
    template_filename?: string;
    confirmed_at?: string | null;
    job_title?: string;
    job_version?: {
      snapshot_id?: string | null;
      run_id?: string | null;
      content_hash: string;
    } | null;
  };
};
export type DocumentPreview = {
  draft: {
    title: string;
    language: string;
    sections: {
      heading: string;
      paragraphs: { text: string; fact_ids: string[]; kind: string }[];
    }[];
  };
  facts: { id: string; text: string; source: string }[];
};
export type Job = {
  id: string;
  needs_refresh?: boolean;
  title: string;
  company: string;
  russian_company?: boolean;
  location: string;
  url: string;
  description: string;
  salary: string | null;
  score: number;
  match_reasons: string[];
  published_at: string | null;
  last_seen_at?: string | null;
  fetched_at?: string | null;
  freshness?: string;
  location_status?: string;
  details: {
    languages: string[];
    programming_languages: string[];
    qa_types: string[];
    responsibilities: string[];
    requirements: string[];
  };
  applied_at: string | null;
  archived_at: string | null;
  documents: Task[];
};
export type JobPage = {
  items: Job[];
  total: number;
  page: number;
  pages: number;
  snapshot_id: string;
  snapshot_at?: string | null;
  last_search_at?: string | null;
  search_run?: SearchRun | null;
  latest_run_id?: string | null;
};
export type Message = {
  id: string;
  role: string;
  text: string;
  status?: string;
  created_at: string;
};
export type View = "active" | "applied" | "archive" | "profile";

export type SearchRun = {
  id: string;
  created_at: string;
  completed_at: string | null;
  profile_version?: string | null;
  state?: string;
  published?: boolean;
  task_status: string;
  task_error?: string | null;
  count?: number;
  errors?: string[];
  sources?: {
    id: string;
    name: string;
    fetched_at: string;
    count?: number;
    error: string | null;
    limited: boolean;
    cached: boolean;
  }[];
};
