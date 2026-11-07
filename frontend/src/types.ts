export type Session = {
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
  title: string;
  company: string;
  location: string;
  url: string;
  description: string;
  salary: string | null;
  score: number;
  match_reasons: string[];
  published_at: string | null;
  last_seen_at?: string | null;
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
};
export type Message = {
  id: string;
  role: string;
  text: string;
  status?: string;
  created_at: string;
};
export type View = "active" | "applied" | "archive" | "profile";
