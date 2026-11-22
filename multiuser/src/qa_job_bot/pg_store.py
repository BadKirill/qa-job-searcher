from qa_job_bot.pg_document_context import PersonalDocumentContext
from qa_job_bot.pg_documents import PersonalDocuments
from qa_job_bot.pg_jobs import PersonalJobs
from qa_job_bot.pg_preferences import PersonalPreferences
from qa_job_bot.pg_search_runs import PersonalSearchRuns
from qa_job_bot.pg_tasks import PersonalTasks


class PostgresStore(
    PersonalJobs,
    PersonalDocuments,
    PersonalTasks,
    PersonalPreferences,
    PersonalSearchRuns,
    PersonalDocumentContext,
):
    pass
