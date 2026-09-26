"""
scraper.py
----------
Pulls every open role from micro1's public job portal API and writes:

  - micro1-jobs.json  (slim job data, read by WordPress via GitHub raw)
  - status.json       (last_run, last_success, job_count, new/removed
                        counts, status, error_message — read by the
                        TFAI Status menu bar app; same schema as Turing's)

No login needed. Only two read-only actions are ever called:
  - get_all_jobs_for_site_map   (one call, whole list, no pay)
  - get_job_description         (one call per job, only for job_ids that
                                 are not already stored in micro1-jobs.json)

On ANY error the previous micro1-jobs.json is left untouched and
status.json is written with status "error".

Run manually:
    python3 scraper.py              # scrape, write files, git push
    python3 scraper.py --no-push    # scrape and write files only

Run on cron via run.sh, same pattern as the Mercor/Turing scrapers.
"""

import os
import re
import sys
import json
import time
import html
import subprocess
import urllib.request
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR         = os.environ.get("MICRO1_OUTPUT_DIR", HERE)
JOBS_OUTPUT_PATH   = os.path.join(OUTPUT_DIR, "micro1-jobs.json")
STATUS_OUTPUT_PATH = os.path.join(OUTPUT_DIR, "status.json")
PAUSE_FLAG_PATH    = os.path.join(OUTPUT_DIR, ".paused")

API_URL           = "https://prod-api.micro1.ai/api/v1/job/portal"
REQUEST_DELAY_SEC = float(os.environ.get("MICRO1_REQUEST_DELAY", "0.5"))  # throttle between detail calls
REQUEST_TIMEOUT   = 30
MAX_ATTEMPTS      = 3
SOURCE_NAME       = "micro1"

# Public referral code (it is visible in every apply link on talentsforai.com).
REFERRAL_URL_TEMPLATE = (
    "https://jobs.micro1.ai/post/{job_id}"
    "?referralCode=e1b5c4f0-aaa5-4d6b-a6e7-5dd6ab2ae21c"
    "&utm_source=referral&utm_medium=share&utm_campaign=job_referral"
)

# Fields fetched via get_job_description; a stored job missing any of these
# is treated as not-yet-detailed and re-fetched.
DETAIL_KEYS = ("hourly_min", "hourly_max", "monthly_min", "monthly_max", "yearly", "location_raw")

# Only these two read-only actions may ever be sent. Everything else
# (apply_job, get_all_jobs, ...) is refused before a request is made.
ALLOWED_ACTIONS = ("get_all_jobs_for_site_map", "get_job_description")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Status / previous-run state ─────────────────────────────────────
def load_previous_status():
    if os.path.exists(STATUS_OUTPUT_PATH):
        try:
            with open(STATUS_OUTPUT_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def load_previous_jobs():
    """Return {job_id: job_dict} from the existing micro1-jobs.json (the detail cache)."""
    if os.path.exists(JOBS_OUTPUT_PATH):
        try:
            with open(JOBS_OUTPUT_PATH, "r") as f:
                data = json.load(f)
            return {job["job_id"]: job for job in data.get("jobs", []) if job.get("job_id")}
        except Exception:
            return {}
    return {}


def write_status(status, job_count=None, new_jobs=None, removed_jobs=None, error_message=None):
    prev = load_previous_status()
    payload = {
        "source": SOURCE_NAME,
        "last_run": now_iso(),
        "last_success": now_iso() if status == "ok" else prev.get("last_success"),
        "job_count": job_count if job_count is not None else prev.get("job_count", 0),
        "new_jobs": new_jobs if new_jobs is not None else 0,
        "removed_jobs": removed_jobs if removed_jobs is not None else 0,
        "status": status,
        "error_message": error_message,
    }
    with open(STATUS_OUTPUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    return payload


def git_push():
    """Push the updated micro1-jobs.json to GitHub, mirroring the Turing/Mercor git_push()."""
    d = OUTPUT_DIR
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subprocess.run(["git", "-C", d, "add", "micro1-jobs.json"])
    if subprocess.run(["git", "-C", d, "diff", "--staged", "--quiet"]).returncode == 0:
        print("No changes to push")
        return
    subprocess.run(["git", "-C", d, "commit", "-m", f"chore: sync {ts}"])
    if subprocess.run(["git", "-C", d, "push"]).returncode == 0:
        print("Pushed OK")
    else:
        print("WARNING: git push failed (data was still written locally)")


# ── micro1 API ──────────────────────────────────────────────────────
def api_call(body):
    """POST one whitelisted read-only action. Retries transient failures."""
    action = body.get("action")
    if action not in ALLOWED_ACTIONS:
        raise RuntimeError(f"refusing to call non-whitelisted action {action!r}")

    payload = json.dumps(body).encode()
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(
                API_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (compatible; TalentsForAI-job-sync)",
                },
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                raise RuntimeError("unexpected response shape")
            return data
        except Exception as e:
            last_err = e
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 * attempt)
    raise RuntimeError(f"{action} failed after {MAX_ATTEMPTS} attempts: {last_err}")


def fetch_job_list():
    resp = api_call({"action": "get_all_jobs_for_site_map"})
    jobs = resp.get("data")
    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("get_all_jobs_for_site_map returned no jobs (refusing to overwrite the board)")
    return jobs


def fetch_job_detail(job_id):
    resp = api_call({"action": "get_job_description", "job_id": job_id})
    data = resp.get("data")
    return data if isinstance(data, dict) else None


# ── Parsing ─────────────────────────────────────────────────────────
def extract_location_raw(description_html):
    """Full text after the first 'Location:' (or Portuguese 'Localização:') label, or None.

    Descriptions are HTML ("<p><strong>Location:</strong> United States only</p>"),
    so strip tags, then look for a block that starts with the label.
    """
    if not description_html:
        return None
    text = re.sub(r"(?i)</p>|<br\s*/?>|</li>|</h\d>", "\n", description_html)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ")
    for block in text.split("\n"):
        m = re.match(r"(?i)\s*(?:location|localiza[cç][aã]o)\s*:\s*(.*)$", block)
        if m:
            value = m.group(1).strip()
            return value or None
    return None


def detail_fields(detail):
    rate = detail.get("ideal_hourly_rate") or {}
    return {
        "hourly_min":  rate.get("min"),
        "hourly_max":  rate.get("max"),
        "monthly_min": detail.get("ideal_monthly_salary_min"),
        "monthly_max": detail.get("ideal_monthly_salary_max"),
        "yearly":      detail.get("ideal_yearly_compensation"),  # raw, whatever the API gives
        "location_raw": extract_location_raw(detail.get("job_description")),
    }


def build_job(list_item, fields):
    job_id = list_item["job_id"]
    return {
        "job_id":       job_id,
        "title":        list_item.get("job_name"),
        "domain_slug":  list_item.get("domain_slug"),
        "date_posted":  list_item.get("date_posted"),
        "skills":       list_item.get("skills") or [],
        "hourly_min":   fields["hourly_min"],
        "hourly_max":   fields["hourly_max"],
        "monthly_min":  fields["monthly_min"],
        "monthly_max":  fields["monthly_max"],
        "yearly":       fields["yearly"],
        "location_raw": fields["location_raw"],
        "referral_url": REFERRAL_URL_TEMPLATE.format(job_id=job_id),
    }


def write_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ── Main ────────────────────────────────────────────────────────────
def main():
    push = "--no-push" not in sys.argv and not os.environ.get("MICRO1_NO_PUSH")

    if os.path.exists(PAUSE_FLAG_PATH):
        print("Paused — skipping this run.")
        write_status("paused")
        return

    started = time.time()
    prev = load_previous_jobs()

    try:
        print("Fetching job list...")
        listing = fetch_job_list()
        print(f"  {len(listing)} jobs in list; {sum(1 for j in listing if j.get('job_id') in prev)} already stored")

        jobs = []
        fetched = 0
        skipped_closed = 0
        for item in listing:
            job_id = item.get("job_id")
            if not job_id:
                continue

            cached = prev.get(job_id)
            if cached and all(k in cached for k in DETAIL_KEYS):
                fields = {k: cached[k] for k in DETAIL_KEYS}
            else:
                detail = fetch_job_detail(job_id)
                fetched += 1
                if fetched % 25 == 0:
                    print(f"  ...{fetched} detail calls done")
                time.sleep(REQUEST_DELAY_SEC)
                if detail is None:
                    print(f"  [warn] no detail data for {job_id} — skipped this run")
                    continue
                if detail.get("job_status") != "open":
                    skipped_closed += 1
                    continue
                fields = detail_fields(detail)

            jobs.append(build_job(item, fields))

        if not jobs:
            raise RuntimeError("no open jobs after processing (refusing to overwrite the board)")

        new_ids = {j["job_id"] for j in jobs}
        new_count = len(new_ids - set(prev))
        removed_count = len(set(prev) - new_ids)

        write_json_atomic(JOBS_OUTPUT_PATH, {
            "updated_at": now_iso(),
            "count": len(jobs),
            "jobs": jobs,
        })
        write_status("ok", job_count=len(jobs), new_jobs=new_count, removed_jobs=removed_count)

        with_pay = sum(1 for j in jobs if j["hourly_max"])
        print(f"\nDone in {time.time() - started:.1f}s. {len(jobs)} jobs written to {JOBS_OUTPUT_PATH}")
        print(f"Detail calls: {fetched} | with hourly pay: {with_pay} | closed skipped: {skipped_closed}")
        print(f"New since last run: {new_count} | Removed since last run: {removed_count}")

        if push:
            git_push()
        else:
            print("--no-push: not pushing")

    except Exception as e:
        print(f"ERROR: {e} — previous micro1-jobs.json left untouched")
        write_status("error", error_message=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
