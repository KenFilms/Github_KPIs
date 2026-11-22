#!/usr/bin/env python3
"""
Collect daily GitHub traffic (views + clones) for all of your repositories and
merge it into a growing local history.

GitHub only keeps the last 14 days of traffic, so this script is meant to run
regularly (daily is ideal, at least once every 14 days is required to avoid gaps).
Every run re-reads the full 14-day window and merges it into data/traffic.json,
so a few missed days are filled in automatically by the next run.

It also records firmware use:
  release downloads   GitHub only reports an all-time download_count per release
                      file, so each run stores the counts and books the increase
                      since the previous run as that day's downloads.
  browser flashes     read from the access log of the small ping endpoint that the
                      web-flasher pages report to (--ping-log; see README.md).

And referring sites and popular content: GitHub only reports the top 10 referrers and
pages of the last 14 days as one total, with no daily breakdown, so each run saves those
lists as the snapshot of its UTC day (a later run on the same day replaces it).

Website (GitHub Pages) numbers:
  page visits         the same ping endpoint also receives one anonymous ping per page
                      and browser session from the site's pages (deploy/page-counter.html).
  search terms        Google Search Console (clicks, impressions, position per page and
                      search term), read with a service-account key (optional; see README.md).

Outputs
  data/traffic.json   canonical history (the file to back up)
  data/traffic.js     same data wrapped for dashboard.html (works from file://)

Token lookup order (first one found wins)
  1. env TRAFFIC_TOKEN, GITHUB_TOKEN or GH_TOKEN
  2. systemd credential "github-token" (how the server runs it)
  3. macOS Keychain item  "github-traffic-dashboard"
  4. file  .github_token  next to this script
  5. `gh auth token` if the GitHub CLI is installed

The token needs read access to repository traffic:
  fine-grained PAT  -> Repository permissions: Administration = Read-only
  classic PAT       -> scope "repo" (or "public_repo" for public repos only)

Only the Python standard library is used (works with the macOS system python3).
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
KEYCHAIN_SERVICE = "github-traffic-dashboard"
GSC_KEYCHAIN = "github-traffic-dashboard-gsc"
GSC_API = "https://www.googleapis.com/webmasters/v3"
GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GSC_BACKFILL_DAYS = 486   # Search Console keeps 16 months; read all of it the first time
GSC_REFRESH_DAYS = 10     # Google still revises recent days, so re-read these on every run
SCHEMA_VERSION = 1
HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
# Token
# --------------------------------------------------------------------------- #
def find_token():
    for var in ("TRAFFIC_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var, "").strip():
            return os.environ[var].strip(), "env " + var

    # systemd LoadCredential= (used on the server; see deploy/github-traffic.service)
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir and os.path.isfile(os.path.join(cred_dir, "github-token")):
        with open(os.path.join(cred_dir, "github-token"), encoding="utf-8") as fh:
            tok = fh.read().strip()
        if tok:
            return tok, "systemd credential"

    if sys.platform == "darwin" and shutil.which("security"):
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip(), "macOS Keychain"
        except (OSError, subprocess.SubprocessError):
            pass

    token_file = os.path.join(HERE, ".github_token")
    if os.path.isfile(token_file):
        with open(token_file, encoding="utf-8") as fh:
            tok = fh.read().strip()
        if tok:
            return tok, ".github_token file"

    if shutil.which("gh"):
        try:
            out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip(), "gh CLI"
        except (OSError, subprocess.SubprocessError):
            pass

    return None, None


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__("HTTP %s: %s" % (status, message))
        self.status = status


def _request(url, token, retries=3):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-traffic-dashboard",
    })
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return (json.loads(body) if body else None), resp.headers
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")
            try:
                message = json.loads(body).get("message", body)
            except ValueError:
                message = body[:200]
            # Primary rate limit: wait for reset if it is close, otherwise give up.
            if err.code in (403, 429) and err.headers.get("X-RateLimit-Remaining") == "0":
                reset = int(err.headers.get("X-RateLimit-Reset", "0"))
                wait = max(0, reset - int(time.time())) + 2
                if wait <= 300 and attempt < retries:
                    log("rate limited, waiting %ss" % wait)
                    time.sleep(wait)
                    continue
            if err.code >= 500 and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise ApiError(err.code, message)
        except urllib.error.URLError as err:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise ApiError("network", str(err.reason))
    raise ApiError("retry", "gave up after %d attempts" % (retries + 1))


def _next_link(headers):
    for part in (headers.get("Link") or "").split(","):
        if 'rel="next"' in part:
            return part[part.find("<") + 1:part.find(">")]
    return None


def api_get(path, token, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data, _ = _request(url, token)
    return data


def api_get_all(path, token, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    items = []
    while url:
        data, headers = _request(url, token)
        items.extend(data or [])
        url = _next_link(headers)
    return items


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
def load_store(path):
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"schema": SCHEMA_VERSION, "owner": None, "updated_at": None,
            "coverage": [], "repos": {}}


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def save_store(store, data_dir):
    os.makedirs(data_dir, exist_ok=True)
    text = json.dumps(store, indent=1, sort_keys=True, ensure_ascii=False)
    write_atomic(os.path.join(data_dir, "traffic.json"), text + "\n")
    compact = json.dumps(store, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    # Escape "</" so a repo description can never close the <script> tag.
    js = "window.TRAFFIC_DATA = " + compact.replace("</", "<\\/") + ";\n"
    write_atomic(os.path.join(data_dir, "traffic.js"), js)


def merge_days(series, rows):
    """Merge [{timestamp, count, uniques}] into {"YYYY-MM-DD": [count, uniques]}.

    Counts for a past day only grow while GitHub finishes processing, and the
    oldest day of the 14-day window can come back truncated, so we keep the
    maximum ever seen for each day. Returns the number of days that changed.
    """
    changed = 0
    for row in rows:
        day = row["timestamp"][:10]
        new = [int(row.get("count", 0)), int(row.get("uniques", 0))]
        old = series.get(day)
        merged = new if old is None else [max(old[0], new[0]), max(old[1], new[1])]
        if merged != old:
            series[day] = merged
            changed += 1
    return changed


def parse_ts(value):
    """ISO timestamp from GitHub or the store -> aware datetime (or None)."""
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso_ts(moment):
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def spread(days, amount, start, end):
    """Add `amount` to {"YYYY-MM-DD": n}, split over the UTC days of [start, end] by time.

    Runs are a few hours apart, so this mostly lands on one day; after downtime the
    downloads are spread evenly instead of piling up on the day collection resumed.
    Whole numbers only (largest remainder), so the total stays exact.
    """
    if end <= start:
        start = end - dt.timedelta(seconds=1)
    total = (end - start).total_seconds()
    shares, day = [], start.date()
    while day <= end.date():
        lo = max(start, dt.datetime.combine(day, dt.time(), dt.timezone.utc))
        hi = min(end, dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), dt.timezone.utc))
        if hi > lo:
            shares.append([day.isoformat(), amount * (hi - lo).total_seconds() / total])
        day += dt.timedelta(days=1)
    whole = [[d, int(s)] for d, s in shares]
    left = amount - sum(n for _, n in whole)
    order = sorted(range(len(shares)), key=lambda i: shares[i][1] - int(shares[i][1]), reverse=True)
    for i in order[:left]:
        whole[i][1] += 1
    for d, n in whole:
        if n:
            days[d] = days.get(d, 0) + n


def update_assets(entry, releases, now):
    """Snapshot release-file download counts and book the increase per day.

    Returns the number of downloads added in this run.
    """
    assets = entry.setdefault("assets", {})
    prev = parse_ts(entry.get("releases_checked"))
    seen, added = set(), 0
    for rel in releases:
        if rel.get("draft"):
            continue
        published = parse_ts(rel.get("published_at"))
        for a in rel.get("assets") or []:
            aid = str(a["id"])
            seen.add(aid)
            count = int(a.get("download_count") or 0)
            rec = assets.get(aid)
            if rec is None:
                rec = assets[aid] = {"days": {}}
                appeared = max(filter(None, [parse_ts(a.get("created_at")), published]), default=None)
                if prev is not None and appeared is not None and appeared > prev:
                    base, since = 0, appeared      # new since the last run: every download is new
                else:
                    base, since = count, None      # first sighting: this is the baseline
            else:
                base, since = int(rec.get("count") or 0), prev
            rec.update({"name": a.get("name") or "", "tag": rel.get("tag_name") or "",
                        "created_at": (a.get("created_at") or "")[:10],
                        # never lower the baseline, or a dip and recovery would be counted twice
                        "count": max(count, base) if since is not None else count})
            rec.pop("deleted", None)
            if since is not None and count > base:
                spread(rec["days"], count - base, since, now)
                added += count - base
    for aid, rec in assets.items():
        if aid not in seen:
            rec["deleted"] = True
    entry["releases_checked"] = iso_ts(now)
    return added


def read_pings(path, repo_keys):
    """Count flasher-page pings from the ping endpoint's JSON access log.

    repo_keys maps lower-case repo name -> store key. Returns
    {key: {"flashes": {day: [ok, failed]}, "bin": {day: n}, "chips": {chip: ok},
           "visits": {day: {page path: {referring host or "": n}}}}}.
    The log is the source of truth and is re-read in full on every run.
    """
    out = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
                if rec.get("status") not in (200, 204):
                    continue
                uri = urllib.parse.urlsplit((rec.get("request") or {}).get("uri") or "")
                q = urllib.parse.parse_qs(uri.query)
                key = repo_keys.get((q.get("p") or [""])[0].lower())
                if not key or q.get("t"):       # unknown repo, or a test ping
                    continue
                day = dt.datetime.fromtimestamp(float(rec["ts"]), dt.timezone.utc).date().isoformat()
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            r = out.setdefault(key, {"flashes": {}, "bin": {}, "chips": {}, "visits": {}})
            if uri.path == "/view":
                page = ((q.get("u") or ["/"])[0] or "/")[:100]
                if page.endswith("/index.html"):
                    page = page[:-len("index.html")]
                ref = (q.get("r") or [""])[0].lower()[:100]
                cell = r["visits"].setdefault(day, {}).setdefault(page, {})
                cell[ref] = cell.get(ref, 0) + 1
            elif uri.path == "/flash":
                result = (q.get("s") or [""])[0]
                if result not in ("ok", "fail"):
                    continue
                cell = r["flashes"].setdefault(day, [0, 0])
                cell[0 if result == "ok" else 1] += 1
                if result == "ok":
                    chip = (q.get("c") or ["unknown"])[0][:24] or "unknown"
                    r["chips"][chip] = r["chips"].get(chip, 0) + 1
            elif uri.path == "/download":
                r["bin"][day] = r["bin"].get(day, 0) + 1
    return out


def fetch_referrers(full_name, token):
    """Top 10 referring sites of the last 14 days -> [[site, views, unique visitors], ...]."""
    rows = api_get("/repos/%s/traffic/popular/referrers" % full_name, token) or []
    out = [[str(r.get("referrer") or "")[:100], int(r.get("count") or 0), int(r.get("uniques") or 0)]
           for r in rows]
    return sorted((x for x in out if x[0]), key=lambda x: (-x[1], -x[2], x[0].lower()))


def fetch_paths(full_name, token):
    """Top 10 pages of the repo on github.com in the last 14 days
    -> [[path, title, views, unique visitors], ...]."""
    rows = api_get("/repos/%s/traffic/popular/paths" % full_name, token) or []
    out = [[str(r.get("path") or "")[:200], str(r.get("title") or "")[:120],
            int(r.get("count") or 0), int(r.get("uniques") or 0)] for r in rows]
    return sorted((x for x in out if x[0]), key=lambda x: (-x[2], -x[3], x[0]))


def store_snapshot(entry, field, day, rows):
    """Keep rows as the repo's `field` snapshot for `day`; empty snapshots are not stored."""
    snaps = entry.get(field) or {}
    if rows:
        snaps[day] = rows
    else:
        snaps.pop(day, None)
    if snaps:
        entry[field] = snaps
    else:
        entry.pop(field, None)


def merge_max(series, new):
    """Merge new into series, keeping the larger value per key (lists per field, dicts recursively)."""
    for key, val in new.items():
        old = series.get(key)
        if old is None:
            series[key] = val
        elif isinstance(val, dict):
            merge_max(old, val)
        elif isinstance(val, list):
            series[key] = [max(o, v) for o, v in zip(old, val)]
        else:
            series[key] = max(old, val)


def merge_coverage(intervals, start, end):
    """Merge a [start, end] ISO-date interval into a sorted list of intervals."""
    spans = sorted([list(i) for i in intervals] + [[start, end]])
    out = []
    for s, e in spans:
        if out:
            prev_end = dt.date.fromisoformat(out[-1][1])
            if dt.date.fromisoformat(s) <= prev_end + dt.timedelta(days=1):
                out[-1][1] = max(out[-1][1], e)
                continue
        out.append([s, e])
    return out


# --------------------------------------------------------------------------- #
# Google Search Console (search terms for the GitHub Pages site)
# --------------------------------------------------------------------------- #
def find_gsc_key():
    """Service-account key (parsed JSON) for Search Console, or (None, None) if not set up.

    Lookup: env TRAFFIC_GSC_KEY (path to the JSON file), systemd credential "gsc-key",
    macOS Keychain item "github-traffic-dashboard-gsc", file .gsc_key.json next to this script.
    """
    found = []

    def from_file(path, source):
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                found.append((fh.read(), source))
    from_file(os.environ.get("TRAFFIC_GSC_KEY", "").strip(), "env TRAFFIC_GSC_KEY")
    if os.environ.get("CREDENTIALS_DIRECTORY"):
        from_file(os.path.join(os.environ["CREDENTIALS_DIRECTORY"], "gsc-key"), "systemd credential")
    if sys.platform == "darwin" and shutil.which("security"):
        try:
            out = subprocess.run(["security", "find-generic-password", "-s", GSC_KEYCHAIN, "-w"],
                                 capture_output=True, text=True, timeout=10)
            if out.returncode == 0:
                found.append((out.stdout, "macOS Keychain"))
        except (OSError, subprocess.SubprocessError):
            pass
    from_file(os.path.join(HERE, ".gsc_key.json"), ".gsc_key.json file")
    for text, source in found:
        if text.strip():   # the server keeps an empty placeholder until a key is installed
            try:
                key = json.loads(text)
            except ValueError:
                log("search terms: the key from %s is not valid JSON, skipped" % source)
                continue
            if key.get("client_email") and key.get("private_key"):
                return key, source
            log("search terms: the key from %s is not a service-account key, skipped" % source)
    return None, None


def _google(url, token=None, form=None, payload=None, retries=2):
    """POST/GET to a Google API -> parsed JSON. Raises ApiError."""
    headers = {"User-Agent": "github-traffic-dashboard"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = {}
            e = parsed.get("error") if isinstance(parsed, dict) else None
            message = (e.get("message") if isinstance(e, dict)
                       else parsed.get("error_description") or e) or body[:200]
            if (err.code >= 500 or err.code == 429) and attempt < retries:
                time.sleep(2 ** attempt + 1)
                continue
            raise ApiError(err.code, message)
        except urllib.error.URLError as err:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise ApiError("network", str(err.reason))
    raise ApiError("retry", "gave up after %d attempts" % (retries + 1))


def _der(data, i):
    """One DER element at data[i] -> (tag, value bytes, index after it)."""
    tag, n = data[i], data[i + 1]
    i += 2
    if n & 0x80:
        size = n & 0x7F
        n = int.from_bytes(data[i:i + size], "big")
        i += size
    return tag, data[i:i + n], i + n


def rsa_private_key(pem):
    """PEM RSA private key (PKCS#8 as in Google key files, or PKCS#1) -> (n, d)."""
    body = "".join(line for line in pem.strip().splitlines() if not line.startswith("-----"))
    _, seq, _ = _der(base64.b64decode(body), 0)
    if "BEGIN PRIVATE KEY" in pem:      # PKCS#8: version, algorithm, OCTET STRING { RSAPrivateKey }
        _, _, i = _der(seq, 0)
        _, _, i = _der(seq, i)
        _, inner, _ = _der(seq, i)
        _, seq, _ = _der(inner, 0)
    nums, i = [], 0
    while len(nums) < 4:                # RSAPrivateKey: version, n, e, d, ...
        _, val, i = _der(seq, i)
        nums.append(int.from_bytes(val, "big"))
    return nums[1], nums[3]


def rs256(message, key):
    """RSASSA-PKCS1-v1_5 signature with SHA-256 (the JWT "RS256" algorithm)."""
    n, d = key
    size = (n.bit_length() + 7) // 8
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(message).digest()
    padded = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
    return pow(int.from_bytes(padded, "big"), d, n).to_bytes(size, "big")


def google_access_token(key):
    """Exchange a service-account key for a read-only Search Console access token."""
    def b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=")
    now = int(time.time())
    token_uri = key.get("token_uri") or "https://oauth2.googleapis.com/token"
    unsigned = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + b"." + b64(json.dumps({
        "iss": key["client_email"], "scope": GSC_SCOPE, "aud": token_uri,
        "iat": now, "exp": now + 3600}).encode())
    jwt = unsigned + b"." + b64(rs256(unsigned, rsa_private_key(key["private_key"])))
    res = _google(token_uri, form={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                   "assertion": jwt.decode()})
    return res["access_token"]


def gsc_sites(token):
    """Search Console properties shared with the service account, without nested duplicates."""
    entries = (_google(GSC_API + "/sites", token) or {}).get("siteEntry") or []
    urls = sorted(e["siteUrl"] for e in entries if e.get("permissionLevel") != "siteUnverifiedUser")

    def inside(u, other):   # every page of property u also belongs to property `other`
        if u == other or u.startswith("sc-domain:"):
            return False
        if other.startswith("sc-domain:"):
            host, dom = urllib.parse.urlsplit(u).hostname or "", other[len("sc-domain:"):]
            return host == dom or host.endswith("." + dom)
        return u.startswith(other)
    return [u for u in urls if not any(inside(u, o) for o in urls)]


def gsc_rows(token, site, start, end, dims):
    """All Search Analytics rows for the dimensions -> (rows, response metadata)."""
    url = GSC_API + "/sites/" + urllib.parse.quote(site, safe="") + "/searchAnalytics/query"
    rows, meta = [], {}
    while True:
        res = _google(url, token, payload={"startDate": start, "endDate": end, "dimensions": dims,
                                           "dataState": "all", "rowLimit": 25000,
                                           "startRow": len(rows)}) or {}
        got = res.get("rows") or []
        rows.extend(got)
        meta = res.get("metadata") or meta
        if len(got) < 25000:
            return rows, meta


def page_repo(page, by_name):
    """Page URL -> store key of its repository, using the GitHub Pages layout:
    https://user.github.io/Repo/... belongs to Repo, other pages of https://user.github.io/
    to the user.github.io repository. by_name maps lower-case repo name -> key."""
    parts = urllib.parse.urlsplit(page)
    first = parts.path.strip("/").split("/")[0].lower()
    return by_name.get(first) or by_name.get((parts.hostname or "").lower())


def collect_search(store, key, today):
    """Read clicks / impressions per page and search term into the repos ("search"):
    {day: {"t": [clicks, impressions], "q": [[term, page path, clicks, impressions, position]]}}.
    "t" also contains searches that Google does not list (too rare, kept private).
    Returns a summary line for the log.
    """
    token = google_access_token(key)
    sites = gsc_sites(token)
    if not sites:
        return ("search terms: the key works, but no Search Console property is shared with %s"
                % key["client_email"])
    full = not store.get("search_days") or not set(sites) <= set(store.get("search_sites") or [])
    start = (today - dt.timedelta(days=GSC_BACKFILL_DAYS if full else GSC_REFRESH_DAYS)).isoformat()
    end = today.isoformat()
    by_name = {e["name"].lower(): k for k, e in store["repos"].items() if e.get("name")}
    days, unmatched, complete = {}, set(), []

    def cell(k, day):
        return days.setdefault(k, {}).setdefault(day, {"t": [0, 0], "q": []})
    for site in sites:
        totals, meta = gsc_rows(token, site, start, end, ["date", "page"])
        terms, _ = gsc_rows(token, site, start, end, ["date", "page", "query"])
        first_incomplete = (meta or {}).get("firstIncompleteDate")
        complete.append(dt.date.fromisoformat(first_incomplete) - dt.timedelta(days=1)
                        if first_incomplete else today - dt.timedelta(days=3))
        for row in totals:
            day, page = row["keys"]
            k = page_repo(page, by_name)
            if not k:
                unmatched.add(page)
                continue
            t = cell(k, day)["t"]
            t[0] += int(row.get("clicks") or 0)
            t[1] += int(row.get("impressions") or 0)
        for row in terms:
            day, page, term = row["keys"]
            k = page_repo(page, by_name)
            if k:
                cell(k, day)["q"].append([term[:120], (urllib.parse.urlsplit(page).path or "/")[:100],
                                          int(row.get("clicks") or 0), int(row.get("impressions") or 0),
                                          round(float(row.get("position") or 0), 1)])
    for k, per_day in days.items():
        for c in per_day.values():
            c["q"].sort(key=lambda x: (-x[2], -x[3], x[0]))
    # Google's numbers for the window replace what we had (it revises recent days).
    for k, entry in store["repos"].items():
        kept = {d: v for d, v in (entry.get("search") or {}).items() if not start <= d <= end}
        kept.update(days.get(k, {}))
        if kept:
            entry["search"] = kept
        else:
            entry.pop("search", None)
    store["search_sites"] = sites
    store["search_days"] = merge_coverage(store.get("search_days", []), start, end)
    store["search_complete"] = min(complete).isoformat()
    clicks = sum(c["t"][0] for per_day in days.values() for c in per_day.values())
    terms_n = len({q[0] for per_day in days.values() for c in per_day.values() for q in c["q"]})
    msg = "search terms: %s, %s to %s: %d clicks, %d search terms" % (", ".join(sites), start, end, clicks, terms_n)
    if unmatched:
        msg += "; %d pages not matched to a repository (e.g. %s)" % (len(unmatched), sorted(unmatched)[0])
    return msg


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def log(msg):
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (stamp, msg), flush=True)


def main():
    ap = argparse.ArgumentParser(description="Collect GitHub repo traffic (views + clones).")
    ap.add_argument("--data-dir", default=os.path.join(HERE, "data"),
                    help="where traffic.json / traffic.js live (default: ./data)")
    ap.add_argument("--include-forks", action="store_true", help="also track forked repos")
    ap.add_argument("--include-orgs", action="store_true",
                    help="also track org repos you can push to")
    ap.add_argument("--only", nargs="*", metavar="REPO",
                    help="limit to these repo names (e.g. MagicFocus StockWatch)")
    ap.add_argument("--ping-log", metavar="FILE",
                    help="access log of the flasher ping endpoint (browser flashes)")
    args = ap.parse_args()

    token, source = find_token()
    if not token:
        log("No GitHub token found. See README.md -> 'Add your GitHub token'.")
        return 2
    log("token from " + source)

    try:
        me = api_get("/user", token)
    except ApiError as err:
        log("token rejected by GitHub (%s)" % err)
        return 2
    login = me["login"]

    affiliation = "owner,organization_member" if args.include_orgs else "owner"
    repos = api_get_all("/user/repos", token,
                        {"affiliation": affiliation, "per_page": 100, "sort": "full_name"})
    if not args.include_forks:
        repos = [r for r in repos if not r.get("fork")]
    if args.include_orgs:
        repos = [r for r in repos if (r.get("permissions") or {}).get("push")]
    if args.only:
        wanted = {n.lower() for n in args.only}
        repos = [r for r in repos if r["name"].lower() in wanted or r["full_name"].lower() in wanted]
    log("%s: %d repositories to collect" % (login, len(repos)))

    store_path = os.path.join(args.data_dir, "traffic.json")
    store = load_store(store_path)
    store["owner"] = login
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    store.setdefault("firmware_since", iso_ts(now))

    ok, failed, changed_days, downloads, release_errors = 0, [], 0, 0, []
    refs_ok, refs_found, paths_ok, popular_errors = 0, 0, 0, []
    for repo in repos:
        key = str(repo["id"])  # id survives renames
        entry = store["repos"].setdefault(key, {"views": {}, "clones": {}})
        entry.update({
            "name": repo["name"],
            "full_name": repo["full_name"],
            "html_url": repo["html_url"],
            "description": repo.get("description") or "",
            "private": bool(repo.get("private")),
            "fork": bool(repo.get("fork")),
            "archived": bool(repo.get("archived")),
            "stars": repo.get("stargazers_count", 0),
            "forks": repo.get("forks_count", 0),
            "created_at": (repo.get("created_at") or "")[:10],
            "last_seen": today.isoformat(),
        })
        try:
            views = api_get("/repos/%s/traffic/views" % repo["full_name"], token, {"per": "day"})
            clones = api_get("/repos/%s/traffic/clones" % repo["full_name"], token, {"per": "day"})
        except ApiError as err:
            failed.append((repo["full_name"], str(err)))
            continue
        changed_days += merge_days(entry["views"], views.get("views", []))
        changed_days += merge_days(entry["clones"], clones.get("clones", []))
        ok += 1
        # On an error, today's earlier snapshot (if any) is kept.
        try:
            refs = fetch_referrers(repo["full_name"], token)
        except ApiError as err:
            popular_errors.append(("referrers", repo["full_name"], str(err)))
        else:
            store_snapshot(entry, "referrers", today.isoformat(), refs)
            refs_ok += 1
            refs_found += bool(refs)
        try:
            paths = fetch_paths(repo["full_name"], token)
        except ApiError as err:
            popular_errors.append(("popular content", repo["full_name"], str(err)))
        else:
            store_snapshot(entry, "paths", today.isoformat(), paths)
            paths_ok += 1
        try:
            releases = api_get_all("/repos/%s/releases" % repo["full_name"], token, {"per_page": 100})
            downloads += update_assets(entry, releases, now)
        except ApiError as err:
            release_errors.append((repo["full_name"], str(err)))

    pings = None
    if args.ping_log:
        if os.path.isfile(args.ping_log):
            keys = {e["name"].lower(): k for k, e in store["repos"].items() if e.get("name")}
            try:
                pings = read_pings(args.ping_log, keys)
            except OSError as err:
                log("could not read ping log %s: %s" % (args.ping_log, err))
        else:
            log("ping log %s does not exist yet (no flasher pings so far)" % args.ping_log)
    for key, got in (pings or {}).items():
        entry = store["repos"][key]
        merge_max(entry.setdefault("flashes", {}), got["flashes"])
        merge_max(entry.setdefault("bin_clicks", {}), got["bin"])
        merge_max(entry.setdefault("flash_chips", {}), got["chips"])
        if got["visits"]:
            merge_max(entry.setdefault("page_visits", {}), got["visits"])

    search_msg = None
    gsc_key, gsc_source = find_gsc_key()
    if gsc_key and ok:
        try:
            search_msg = collect_search(store, gsc_key, today)
        except (ApiError, KeyError, ValueError, IndexError) as err:
            search_msg = "search terms not collected (key from %s): %s" % (gsc_source, err)

    if ok:
        # Each successful run covers GitHub's 14-day window ending today (UTC).
        start = (today - dt.timedelta(days=13)).isoformat()
        store["coverage"] = merge_coverage(store.get("coverage", []), start, today.isoformat())
        # Days with a referrer / popular-content snapshot; a repo without one on such a day had none.
        if refs_ok:
            store["referrer_days"] = merge_coverage(store.get("referrer_days", []),
                                                    today.isoformat(), today.isoformat())
        if paths_ok:
            store["path_days"] = merge_coverage(store.get("path_days", []),
                                                today.isoformat(), today.isoformat())
        store["updated_at"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        store["schema"] = SCHEMA_VERSION
        save_store(store, args.data_dir)

    log("collected %d/%d repos, %d day-values updated -> %s"
        % (ok, len(repos), changed_days, store_path))
    flashes = sum(sum(v[0] for v in g["flashes"].values()) for g in (pings or {}).values())
    log("release downloads since last run: %d; browser flashes logged so far: %d" % (downloads, flashes))
    log("referring sites: %d of %d repos have referrers in the last 14 days" % (refs_found, refs_ok))
    visits = sum(n for g in (pings or {}).values() for pages in g["visits"].values()
                 for refs in pages.values() for n in refs.values())
    log("page visits logged so far: %d" % visits)
    log(search_msg or "search terms: no Search Console key set up (see README.md)")
    for name, why in failed:
        log("  skipped %s: %s" % (name, why))
    for name, why in release_errors:
        log("  releases of %s not readable: %s" % (name, why))
    for what, name, why in popular_errors:
        log("  %s of %s not readable: %s" % (what, name, why))
    if failed and not ok:
        log("No repo could be read. The token probably lacks traffic access "
            "(fine-grained: Administration = Read-only; classic: repo scope).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
