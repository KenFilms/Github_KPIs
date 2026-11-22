#!/usr/bin/env python3
"""Add or update the page counter (deploy/page-counter.html) in GitHub Pages HTML files.

  python3 deploy/install-counter.py <repo-name> <page.html> [<page.html> ...]

<repo-name> is the GitHub repository the pages belong to; the dashboard counts their visits
for that repository. An earlier copy of the counter (also the older "flash counter") is
replaced; otherwise the counter is inserted right before </body>.

The ping endpoint is https://<COUNT_SITE>/, with COUNT_SITE taken from the environment or
from deploy/server.env (written by deploy.sh setup).
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
START = re.compile(r"<!-- (Page|Flash) counter for the GitHub traffic dashboard")


def count_site():
    """COUNT_SITE from the environment, else from deploy/server.env."""
    site = os.environ.get("COUNT_SITE", "").strip()
    if not site:
        try:
            with open(os.path.join(HERE, "server.env"), encoding="utf-8") as fh:
                for line in fh:
                    key, _, val = line.strip().partition("=")
                    if key == "COUNT_SITE":
                        site = val.strip().strip("'\"")
        except OSError:
            pass
    return site


def install(page, snippet):
    with open(page, encoding="utf-8") as fh:
        text = fh.read()
    found = START.search(text)
    if found:
        end = text.find("</script>", found.end())
        if end < 0:
            raise ValueError("found the counter's comment but not its </script>")
        new, action = text[:found.start()] + snippet + text[end + len("</script>"):], "updated"
    else:
        body = text.lower().rfind("</body>")
        if body < 0:
            raise ValueError("no </body> tag")
        new, action = text[:body] + snippet + "\n" + text[body:], "added"
    if new == text:
        return "already up to date"
    with open(page, "w", encoding="utf-8") as fh:
        fh.write(new)
    return action


def main():
    if len(sys.argv) < 3:
        print(__doc__.strip())
        return 2
    repo, pages = sys.argv[1], sys.argv[2:]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", repo):
        print("error: %r is not a repository name" % repo)
        return 2
    site = count_site()
    if not re.fullmatch(r"[A-Za-z0-9.-]+", site):
        print("error: no ping endpoint. Run deploy/deploy.sh setup first, or set COUNT_SITE=<host>.")
        return 2
    with open(os.path.join(HERE, "page-counter.html"), encoding="utf-8") as fh:
        snippet = fh.read().strip().replace("__REPO__", repo).replace("__ENDPOINT__", "https://%s/" % site)
    failed = 0
    for page in pages:
        try:
            print("%-60s %s (REPO = %s)" % (page, install(page, snippet), repo))
        except (OSError, ValueError) as err:
            print("%-60s error: %s" % (page, err))
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
