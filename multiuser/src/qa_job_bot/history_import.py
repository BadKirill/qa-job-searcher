from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import re
import socket
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import asdict, fields
from datetime import timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from qa_job_bot.config import SearchProfile, load_profile
from qa_job_bot.matching import match_job, rank_jobs
from qa_job_bot.models import Job, utcnow
from qa_job_bot.sources.hirify import HirifySource
from qa_job_bot.sources.remote_com import RemoteComSource
from qa_job_bot.utils import html_to_text, parse_datetime

SINCE = "2026-09-26T14:18:18.239589+00:00"
MAX_BYTES = 2_000_000
HOST_PATHS = {
    "hirify.me": r"/jobs/[a-zA-Z0-9_-]+",
    "himalayas.app": r"/(?:companies/[a-zA-Z0-9_-]+/jobs/[a-zA-Z0-9_-]+|jobs/api/search)",
    "talent-api.remote.com": r"/api/v1/public/jobs/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+",
}


class ImportFailure(ValueError):
    pass


class VacancyRejected(ImportFailure):
    pass


def public_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in HOST_PATHS
        or parsed.netloc != parsed.hostname
        or parsed.fragment
        or not re.fullmatch(HOST_PATHS[parsed.hostname], parsed.path)
        or (parsed.query and parsed.path != "/jobs/api/search")
    ):
        raise ImportFailure("URL is outside the saved public source allowlist")
    return parsed.hostname


async def public_address(host: str) -> str:
    results = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = [result[4][0] for result in results]
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ImportFailure("Public source resolved to a non-public address")
    return addresses[0]


class PublicReader:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.limit = asyncio.Semaphore(4)
        self.paused: dict[str, str] = {}
        self.next_retry_at: dict[str, str] = {}
        self.requests = 0

    async def get(self, url: str) -> str:
        host = public_url(url)
        async with self.limit:
            if host in self.paused:
                raise ImportFailure(self.paused[host])
            address = await public_address(host)
            target = httpx.URL(url).copy_with(host=address)
            self.requests += 1
            async with self.client.stream(
                "GET",
                target,
                headers={"Host": host},
                extensions={"sni_hostname": host},
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    message = f"HTTP {response.status_code}"
                    if response.status_code == 429:
                        delay = response.headers.get("retry-after", "not supplied")
                        message += f"; Retry-After: {delay}; source paused for this run"
                        self.paused[host] = message
                        try:
                            until = (
                                utcnow() + timedelta(seconds=max(0, int(delay)))
                                if delay.isdecimal()
                                else parsedate_to_datetime(delay)
                            )
                            self.next_retry_at[host] = until.isoformat()
                        except (ValueError, TypeError, OverflowError):
                            self.next_retry_at[host] = (
                                utcnow() + timedelta(minutes=30)
                            ).isoformat()
                    raise ImportFailure(message)
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > MAX_BYTES:
                        raise ImportFailure("Public response exceeds the size limit")
                return body.decode("utf-8", errors="replace")


class VacancyPage(HTMLParser):
    void_tags = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.parts: list[str] = []
        self.state_parts: list[str] = []
        self.in_state = False
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "script" and attr.get("id") == "__NUXT_DATA__":
            self.in_state = True
        if self.depth:
            if tag not in self.void_tags:
                self.depth += 1
            if tag in {"script", "style"}:
                self.hidden += 1
            if tag in {"p", "div", "li", "br", "h2", "h3"}:
                self.parts.append("\n")
        elif tag == "div" and "description" in str(attr.get("class") or "").split():
            self.depth = 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.void_tags:
            return
        if tag == "script":
            self.in_state = False
        if self.depth:
            self.depth -= 1
            if tag in {"script", "style"}:
                self.hidden = max(0, self.hidden - 1)
            if tag in {"p", "div", "li", "h2", "h3"}:
                self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_state:
            self.state_parts.append(data)
        if self.depth and not self.hidden:
            self.parts.append(data)

    @property
    def text(self) -> str:
        return "\n".join(
            line for part in "".join(self.parts).splitlines() if (line := " ".join(part.split()))
        )


def _decode(values: list, index: int, depth: int = 0):
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(values):
        raise ImportFailure("Invalid public page metadata reference")
    if depth > 8:
        raise ImportFailure("Public page metadata is too deeply nested")
    value = values[index]
    if isinstance(value, dict):
        return {key: _decode(values, ref, depth + 1) for key, ref in value.items()}
    if isinstance(value, list):
        return [_decode(values, ref, depth + 1) for ref in value]
    return value


def hirify_job(html: str, row: dict) -> tuple[Job, str]:
    page = VacancyPage()
    page.feed(html)
    values = json.loads("".join(page.state_parts))
    if not isinstance(values, list) or len(values) > 30_000:
        raise ImportFailure("Unexpected public page metadata")
    item = next(
        (
            value
            for value in values
            if isinstance(value, dict)
            and "id" in value
            and "slug" in value
            and str(_decode(values, value["id"])) == row["source_id"]
        ),
        None,
    )
    if item is None:
        raise ImportFailure("Matching vacancy is missing")
    keys = (
        "title",
        "company_title",
        "work_format",
        "remote_type",
        "remote_restrictions",
        "allowed_locations",
        "regions",
        "salary",
        "tags",
        "work_type",
        "updated_at",
        "created_at",
        "is_archived",
        "is_scam",
        "is_potential_scam",
        "grades",
    )
    data = {key: _decode(values, item[key]) for key in keys if key in item}
    if data.get("is_archived"):
        raise VacancyRejected("Source explicitly marks vacancy archived")
    if data.get("is_scam") or data.get("is_potential_scam"):
        raise VacancyRejected("Source explicitly flags this vacancy as suspicious")
    if len(page.text) < 80:
        raise ImportFailure("Full public description is missing")
    company = data.get("company_title") or "Company hidden"
    if company.startswith("%"):
        company = "Company hidden"
    formats = data.get("work_format") or []
    workplace = "remote" if "remote" in formats else formats[0] if formats else ""
    tags = tuple(
        str(value.get("name") or "") if isinstance(value, dict) else str(value)
        for value in [*(data.get("tags") or []), *(data.get("grades") or [])]
    )
    job = Job(
        source="Hirify",
        source_id=row["source_id"],
        title=data.get("title") or row["title"],
        company=company,
        location=HirifySource._location(data),
        url=row["url"],
        description=page.text,
        remote=workplace == "remote",
        workplace_type=workplace,
        salary=HirifySource._salary(data.get("salary")),
        tags=tags,
        employment_type=data.get("work_type") or "",
        published_at=parse_datetime(data.get("updated_at") or data.get("created_at")),
    )
    return job, "public_page_available"


def saved_job(row: dict) -> Job | None:
    data = json.loads(row["payload"]) if row.get("payload") else {}
    if len(data.get("description") or "") < 80:
        return None
    allowed = {field.name for field in fields(Job)}
    values = {key: value for key, value in data.items() if key in allowed}
    values["published_at"] = parse_datetime(values.get("published_at"))
    values["tags"] = tuple(values.get("tags") or [])
    return Job(**values)


def read_history(path: Path, since: str) -> list[dict]:
    if parse_datetime(since) is None:
        raise ImportFailure("Invalid history timestamp")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in db.execute(
                """SELECT j.*, MIN(d.delivered_at) AS delivered_at FROM jobs j
            JOIN deliveries d USING(fingerprint) WHERE d.delivered_at>=?
            GROUP BY j.fingerprint ORDER BY delivered_at,j.rowid""",
                (since,),
            )
        ]
        current = db.execute("SELECT value FROM web_settings WHERE key='current_jobs'").fetchone()
        current_ids = set(json.loads(current[0])) if current else set()
        canonical = {
            (row["source"], row["source_id"]): row["fingerprint"]
            for row in db.execute("SELECT fingerprint,source,source_id FROM jobs")
            if row["fingerprint"] in current_ids
        }
        for row in rows:
            row["target_id"] = canonical.get((row["source"], row["source_id"]), row["fingerprint"])
            row["already_current"] = row["target_id"] in current_ids
        return rows


async def restore_job(row: dict, reader: PublicReader) -> tuple[Job | None, str, str]:
    if row["source"].startswith("Telegram"):
        return saved_job(row), "stored_telegram_payload", "not_checked"
    if row["source"] == "Hirify":
        job, status = hirify_job(await reader.get(row["url"]), row)
        return job, "hirify_public_page", status
    if row["source"] == "Remote.com":
        key = row["source_id"]
        item = json.loads(await reader.get(f"{RemoteComSource.endpoint}/{key}"))["data"]
        job = saved_job(row)
        if job is None:
            raise ImportFailure("Remote.com saved metadata is missing")
        job.description = html_to_text(item.get("description"))
        job.location = RemoteComSource._location(item)
        job.remote = (item.get("workplace_location") or {}).get("type") == "remote"
        job.workplace_type = "remote" if job.remote else ""
        if len(job.description) < 80:
            raise ImportFailure("Remote.com public description is missing")
        return job, "remote_public_api", "public_api_available"
    if row["source"] == "Himalayas":
        url = str(
            httpx.URL(
                "https://himalayas.app/jobs/api/search",
                params={
                    "q": row["title"],
                    "page": 1,
                },
            )
        )
        items = json.loads(await reader.get(url)).get("jobs", [])
        item = next((item for item in items if item.get("guid") == row["source_id"]), None)
        if item is None or len(html_to_text(item.get("description"))) < 80:
            raise ImportFailure("Full matching Himalayas description unavailable from public API")
        job = Job(
            source="Himalayas",
            source_id=row["source_id"],
            title=item["title"],
            company=item.get("companyName") or row["company"],
            location=", ".join(item.get("locationRestrictions") or []) or "Worldwide",
            url=row["url"],
            description=html_to_text(item["description"]),
            remote=True,
            workplace_type="remote",
            employment_type=item.get("employmentType") or "",
        )
        return job, "himalayas_public_api", "public_api_available"
    raise ImportFailure("Unsupported historical source")


def profile_hash(profile: SearchProfile) -> str:
    return sha256(json.dumps(asdict(profile), sort_keys=True, default=str).encode()).hexdigest()


async def build_report(
    rows: list[dict],
    profile: SearchProfile,
    reader: PublicReader,
    previous: dict | None = None,
) -> dict:
    seen: dict[tuple[str, str], str] = {}
    entries: list[dict] = []
    tasks: list[tuple[dict, dict]] = []
    cached = (
        {
            entry["id"]: entry
            for entry in (previous or {}).get("entries", [])
            if entry.get("status") in {"accepted", "rejected"} and not entry.get("retrieval_error")
        }
        if (previous or {}).get("profile_hash") == profile_hash(profile)
        else {}
    )
    for row in rows:
        entry = {
            "id": row["fingerprint"],
            "source": row["source"],
            "source_id": row["source_id"],
            "title": row["title"],
            "url": row["url"],
            "delivered_at": row["delivered_at"],
            "target_id": row.get("target_id", row["fingerprint"]),
            "already_current": row.get("already_current", False),
        }
        entries.append(entry)
        identity = (row["source"], row["source_id"])
        if identity in seen:
            entry.update(status="duplicate", duplicate_of=seen[identity])
        else:
            seen[identity] = entry["id"]
            tasks.append((row, entry))

    async def restore(row: dict, entry: dict) -> None:
        prior = cached.get(entry["id"])
        if prior and prior.get("url") == entry["url"]:
            entry.update(
                {
                    key: value
                    for key, value in prior.items()
                    if key not in {"target_id", "already_current"}
                }
            )
            entry["reused_from"] = previous["checked_at"]
            return
        try:
            job, method, availability = await restore_job(row, reader)
        except VacancyRejected as error:
            entry.update(status="rejected", availability="closed_or_flagged", reasons=[str(error)])
            return
        except (ImportFailure, httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as error:
            entry["retrieval_error"] = str(error)
            job, method, availability = saved_job(row), "stored_payload_fallback", "unknown"
        entry.update(method=method, availability=availability)
        if job is None:
            entry.update(status="unavailable", reasons=["Full vacancy description unavailable"])
            return
        entry["job"] = asdict(job)
        result = match_job(job, profile)
        ranked = rank_jobs([job], profile)
        if not ranked or result.location_status != "allowed":
            entry.update(
                status="rejected", reasons=result.reasons, location_status=result.location_status
            )
            return
        entry.update(
            status="accepted",
            reasons=result.reasons,
            job=asdict(job),
            description_sha256=sha256(job.description.encode()).hexdigest(),
        )

    await asyncio.gather(*(restore(row, entry) for row, entry in tasks))
    counts = dict(Counter(x["status"] for x in entries))
    counts["already_current"] = sum(
        entry["status"] == "accepted" and entry["already_current"] for entry in entries
    )
    counts["new"] = counts.get("accepted", 0) - counts["already_current"]
    counts["closed_or_flagged"] = sum(
        entry.get("availability") == "closed_or_flagged" for entry in entries
    )
    return {
        "checked_at": utcnow().isoformat(),
        "counts": counts,
        "total": len(entries),
        "entries": entries,
        "profile_hash": profile_hash(profile),
        "http_requests": reader.requests,
        "paused_sources": reader.paused,
        "next_retry_at": reader.next_retry_at,
    }


def apply_report(path: Path, report: dict) -> list[str]:
    accepted = [entry for entry in report["entries"] if entry["status"] == "accepted"]
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT value FROM web_settings WHERE key='imported_jobs'").fetchone()
        ids = set(json.loads(previous[0])) if previous else set()
        for entry in accepted:
            job = entry["job"]
            target_id = entry["target_id"]
            row = db.execute(
                "SELECT source,source_id FROM jobs WHERE fingerprint=?", (target_id,)
            ).fetchone()
            if row != (entry["source"], entry["source_id"]):
                raise ImportFailure("Historical identity changed before import")
            db.execute(
                """UPDATE jobs SET title=?,company=?,location=?,score=?,published_at=?,payload=?
                WHERE fingerprint=?""",
                (
                    job["title"],
                    job["company"],
                    job["location"],
                    job["score"],
                    str(job["published_at"]) if job["published_at"] else None,
                    json.dumps(job, ensure_ascii=False, default=str),
                    target_id,
                ),
            )
            ids.add(target_id)
        result = sorted(ids)
        db.execute(
            "INSERT OR REPLACE INTO web_settings VALUES ('imported_jobs',?)", (json.dumps(result),)
        )
        db.execute("""INSERT INTO web_settings VALUES ('revision','1')
            ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1""")
    return result


async def run(args: argparse.Namespace) -> dict:
    rows = read_history(args.database, args.since)
    previous = json.loads(args.resume.read_text()) if args.resume else None
    for until in (previous or {}).get("next_retry_at", {}).values():
        parsed = parse_datetime(until)
        if parsed is None or parsed > utcnow():
            raise ImportFailure(f"Source retry is not permitted before {until}")
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        report = await build_report(
            rows, load_profile(args.profile), PublicReader(client), previous
        )
    report["since"] = args.since
    report["applied"] = bool(args.apply)
    if args.apply:
        report["imported_ids"] = apply_report(args.database, report)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    args.report.chmod(0o600)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore and revalidate delivered vacancy history")
    parser.add_argument("--database", type=Path, default=Path("data/jobs.sqlite3"))
    parser.add_argument("--profile", type=Path, default=Path("config/search_profile.toml"))
    parser.add_argument("--since", default=SINCE)
    parser.add_argument("--report", type=Path, default=Path("data/history-import-report.json"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resume", type=Path)
    report = asyncio.run(run(parser.parse_args()))
    print(json.dumps({"total": report["total"], **report["counts"], "applied": report["applied"]}))


if __name__ == "__main__":
    main()
