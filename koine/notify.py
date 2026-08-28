"""Proactive drift notifications: when a push leaves translations out of date,
tell a human where -- as a PR/commit comment, a Slack message, and an email.

This is a narrator, not a decider. It reports the work queue the deterministic
engine already computed (`webhook.compute_work_queue`); it never inspects a
translation or forms a verdict, and there is no model here. Swapping or removing
it changes whether people are told, never what koine decided.

Stdlib only, like the rest of the always-on service: Slack via an incoming
webhook (`urllib`), email via `smtplib`, GitHub PR/commit comments via the REST
API (`urllib`). Every channel is independent and degrades honestly -- a channel
with no configuration is skipped, a channel that fails is reported and takes down
neither the others nor the webhook response. Secrets come only from the
environment and are never logged or placed in an error string.

Trust boundary: the report body is built from the engine's *structured* queue
(doc / lang / block / reason). The one untrusted input -- the changed file paths
from the push payload -- is escaped and capped before it can reach a message, so
a crafted path cannot inject formatting or unbounded content into a comment.
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage

GITHUB_API = "https://api.github.com"
_MAX_PATHS = 20
_MAX_ITEMS_PER_LANG = 50
_TIMEOUT = 10

# Plain-language "why" for each drift reason, so a report explains *why* a block
# is out of date instead of only printing its state code. The mechanical work
# queue yields only these two reasons (see webhook.compute_work_queue).
_WHY = {
    "STALE": "source edited after the translation was sealed",
    "UNTRANSLATED": "no translation exists for this block yet",
}


def _esc(s: str) -> str:
    """Neutralize an untrusted string for safe embedding in a message: drop
    non-printable characters and backticks, cap the length. Never trust a path
    from the push payload to be well-formed or benign."""
    return "".join(c for c in str(s) if c.isprintable() and c != "`")[:200]


# --------------------------------------------------------------------------
# Report: a pure function of the queue. No I/O, no secrets, no model.
# --------------------------------------------------------------------------

@dataclass
class Report:
    subject: str
    markdown: str   # for GitHub comments
    text: str       # for Slack and email
    count: int


def format_report(changed_paths, work_queue, *, repo=None, branch=None) -> Report:
    """Human-readable drift report from the mechanical queue. Pure."""
    n = len(work_queue)
    by_lang: dict = {}
    for it in work_queue:
        by_lang.setdefault(it["lang"], []).append(it)

    where = f"{repo}@{branch}" if repo and branch else (repo or "the repository")
    where = _esc(where)
    subject = f"koine: {n} translation block(s) drifted in {where}"

    md = ["### koine drift report", "",
          f"A push to **{where}** left **{n}** translation block(s) out of date:", ""]
    txt = ["koine drift report",
           f"A push to {where} left {n} translation block(s) out of date:", ""]
    for lang in sorted(by_lang):
        items = by_lang[lang][:_MAX_ITEMS_PER_LANG]
        rendered = ", ".join(
            f"block {i['block_index']} ({i['reason']}"
            + (f" — {_WHY[i['reason']]}" if i['reason'] in _WHY else "") + ")"
            for i in items)
        extra = len(by_lang[lang]) - len(items)
        if extra > 0:
            rendered += f", and {extra} more"
        md.append(f"- **{_esc(lang)}**: {rendered}")
        txt.append(f"  {_esc(lang)}: {rendered}")

    if changed_paths:
        shown = [_esc(p) for p in list(changed_paths)[:_MAX_PATHS]]
        more = " ..." if len(changed_paths) > _MAX_PATHS else ""
        md += ["", "Changed paths: " + ", ".join(f"`{p}`" for p in shown) + more]
        txt += ["", "Changed paths: " + ", ".join(shown) + more]

    tail = ("koine derived this mechanically from the sealed ledger; "
            "the model was not consulted.")
    md += ["", f"_{tail}_"]
    txt += ["", tail]
    return Report(subject=subject, markdown="\n".join(md),
                  text="\n".join(txt), count=n)


# --------------------------------------------------------------------------
# Channels. Each takes an injectable transport so it is testable without a
# network, and each raises on failure (the dispatcher catches).
# --------------------------------------------------------------------------

def send_slack(webhook_url: str, report: Report, *, opener=urllib.request.urlopen) -> None:
    body = json.dumps({"text": report.text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with opener(req, timeout=_TIMEOUT):
        pass


def send_email(cfg: "NotifyConfig", report: Report, *, smtp_factory=None) -> None:
    msg = EmailMessage()
    msg["From"] = cfg.mail_from
    msg["To"] = cfg.mail_to
    msg["Subject"] = report.subject
    msg.set_content(report.text)
    if smtp_factory is None:
        smtp_factory = smtplib.SMTP_SSL if cfg.smtp_ssl else smtplib.SMTP
    with smtp_factory(cfg.smtp_host, cfg.smtp_port, timeout=_TIMEOUT) as s:
        if cfg.smtp_starttls and not cfg.smtp_ssl:
            s.starttls(context=ssl.create_default_context())
        if cfg.smtp_user:
            s.login(cfg.smtp_user, cfg.smtp_password)
        s.send_message(msg)


def _gh(method: str, path: str, token: str, *, data=None,
        opener=urllib.request.urlopen):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(GITHUB_API + path, data=body, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "koine-notify",
        "Content-Type": "application/json",
    })
    with opener(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def find_open_pr(repo: str, branch: str, token: str, *, opener=urllib.request.urlopen):
    """PR number of the open PR whose head is `branch`, or None."""
    owner = repo.split("/")[0]
    prs = _gh("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}",
              token, opener=opener)
    return prs[0]["number"] if prs else None


def notify_github(repo: str, branch, sha, report: Report, token: str,
                  *, pr_number=None, opener=urllib.request.urlopen) -> str:
    """Comment on the PR for this change if there is one, else on the commit.
    A known `pr_number` (e.g. from a GitHub Actions event) is used directly and
    is more reliable than a head lookup, which fails for forked branches.
    Returns a short label of what it did. Raises if no target exists."""
    pr = pr_number
    if pr is None and branch:
        pr = find_open_pr(repo, branch, token, opener=opener)
    if pr is not None:
        _gh("POST", f"/repos/{repo}/issues/{pr}/comments", token,
            data={"body": report.markdown}, opener=opener)
        return f"pr#{pr}"
    if sha:
        _gh("POST", f"/repos/{repo}/commits/{sha}/comments", token,
            data={"body": report.markdown}, opener=opener)
        return f"commit {sha[:7]}"
    raise ValueError("no open PR and no commit SHA to comment on")


# --------------------------------------------------------------------------
# Configuration (env only) and dispatch (honest degradation, never raises).
# --------------------------------------------------------------------------

@dataclass
class NotifyConfig:
    slack_url: str = ""
    github_token: str = ""
    smtp_host: str = ""
    smtp_port: int = 0
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_ssl: bool = False
    smtp_starttls: bool = True
    mail_from: str = ""
    mail_to: str = ""

    @classmethod
    def from_env(cls, env=None) -> "NotifyConfig":
        env = os.environ if env is None else env

        def flag(key, default):
            return env.get(key, str(default)).strip().lower() in ("1", "true", "yes", "on")

        try:
            port = int(env.get("KOINE_SMTP_PORT", "0") or "0")
        except ValueError:
            port = 0
        return cls(
            slack_url=env.get("KOINE_SLACK_WEBHOOK_URL", ""),
            github_token=env.get("KOINE_GITHUB_TOKEN", ""),
            smtp_host=env.get("KOINE_SMTP_HOST", ""),
            smtp_port=port,
            smtp_user=env.get("KOINE_SMTP_USER", ""),
            smtp_password=env.get("KOINE_SMTP_PASSWORD", ""),
            smtp_ssl=flag("KOINE_SMTP_SSL", False),
            smtp_starttls=flag("KOINE_SMTP_STARTTLS", True),
            mail_from=env.get("KOINE_MAIL_FROM", ""),
            mail_to=env.get("KOINE_MAIL_TO", ""),
        )

    def email_ready(self) -> bool:
        return bool(self.smtp_host and self.mail_from and self.mail_to)

    def any(self) -> bool:
        return bool(self.slack_url or self.github_token or self.email_ready())


def dispatch(report: Report, cfg: NotifyConfig, *, context=None,
             slack_opener=urllib.request.urlopen,
             gh_opener=urllib.request.urlopen,
             smtp_factory=None) -> dict:
    """Fire every configured channel. Never raises. Returns {channel: status}.
    A status is 'sent', 'sent (<detail>)', 'skipped[: reason]', or
    'failed: <ExceptionType>' -- error strings carry only the exception type,
    never the message, so a secret in a URL or token cannot leak into a log."""
    context = context or {}
    out: dict = {}

    if cfg.slack_url:
        try:
            send_slack(cfg.slack_url, report, opener=slack_opener)
            out["slack"] = "sent"
        except Exception as e:  # noqa: BLE001 - a channel failure must be contained
            out["slack"] = f"failed: {type(e).__name__}"
    else:
        out["slack"] = "skipped"

    if cfg.email_ready():
        try:
            send_email(cfg, report, smtp_factory=smtp_factory)
            out["email"] = "sent"
        except Exception as e:  # noqa: BLE001
            out["email"] = f"failed: {type(e).__name__}"
    else:
        out["email"] = "skipped"

    if cfg.github_token and context.get("repo"):
        try:
            did = notify_github(context["repo"], context.get("branch"),
                                context.get("sha"), report, cfg.github_token,
                                pr_number=context.get("pr_number"), opener=gh_opener)
            out["github"] = f"sent ({did})"
        except Exception as e:  # noqa: BLE001
            out["github"] = f"failed: {type(e).__name__}"
    elif cfg.github_token:
        out["github"] = "skipped: no repo context"
    else:
        out["github"] = "skipped"

    return out


def maybe_notify(result: dict, cfg: NotifyConfig | None, **transports) -> dict:
    """Notify only when there is drift and at least one channel is configured.
    Never raises: notifications must never break the webhook response."""
    if cfg is None or not cfg.any():
        return {}
    queue = result.get("work_queue") or []
    if not queue:
        return {}
    ctx = result.get("context") or {}
    try:
        report = format_report(result.get("changed_paths") or [], queue,
                               repo=ctx.get("repo"), branch=ctx.get("branch"))
        return dispatch(report, cfg, context=ctx, **transports)
    except Exception as e:  # noqa: BLE001 - contain everything
        return {"error": type(e).__name__}
