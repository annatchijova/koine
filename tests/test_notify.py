"""Notifier tests. Every channel takes an injectable transport, so delivery is
exercised without a network; the report formatting and the honest-degradation
of the dispatcher are pinned directly.
"""
import json

import pytest

from koine import notify
from koine.notify import NotifyConfig, Report, dispatch, format_report, maybe_notify

QUEUE = [
    {"doc": "README.md", "lang": "es", "block_index": 1, "reason": "STALE"},
    {"doc": "README.md", "lang": "es", "block_index": 4, "reason": "UNTRANSLATED"},
    {"doc": "README.md", "lang": "ru", "block_index": 1, "reason": "UNTRANSLATED"},
]


# ---------- report formatting (pure) ----------

def test_report_summarizes_queue_by_language():
    r = format_report(["README.md"], QUEUE, repo="annatchijova/koine", branch="main")
    assert r.count == 3
    assert "annatchijova/koine@main" in r.text
    assert "block 1 (STALE)" in r.markdown
    assert "es" in r.markdown and "ru" in r.markdown
    # the narration must state the model was not consulted
    assert "model was not consulted" in r.text


def test_report_escapes_untrusted_paths():
    # a crafted path must not inject backticks/newlines/control chars into a message
    evil = "evil`\n```@everyone\x07" + "A" * 500
    r = format_report([evil], QUEUE, repo="r", branch="b")
    # the untrusted content cannot inject a backtick, a code fence, or a control
    # char (only koine's own single wrapping backticks remain)
    assert "evil`" not in r.markdown
    assert "```" not in r.markdown
    assert "\x07" not in r.markdown and "\n```" not in r.markdown
    # and it is capped, not unbounded
    assert len(r.markdown) < 4000


def test_report_caps_number_of_paths():
    r = format_report([f"f{i}.md" for i in range(100)], QUEUE)
    assert "..." in r.markdown  # truncation is disclosed, not silent


# ---------- fake transports ----------

class _Resp:
    def __init__(self, body=b""):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    """Records requests; returns `get_body(req)` (default empty)."""
    def __init__(self, get_body=None, fail=False):
        self.calls = []
        self._get_body = get_body or (lambda req: b"")
        self._fail = fail

    def __call__(self, req, timeout=None):
        self.calls.append(req)
        if self._fail:
            raise ConnectionError("https://hooks.slack.com/services/SUPER-SECRET-TOKEN")
        return _Resp(self._get_body(req))


class _SMTP:
    last = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started = False
        self.logged = None
        self.sent = None
        _SMTP.last = self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.started = True

    def login(self, u, p):
        self.logged = (u, p)

    def send_message(self, msg):
        self.sent = msg


# ---------- channels ----------

def test_send_slack_posts_json_text():
    op = _Opener()
    notify.send_slack("https://hooks.slack.test/x", format_report([], QUEUE), opener=op)
    req = op.calls[0]
    assert req.get_method() == "POST"
    assert json.loads(req.data)["text"].startswith("koine drift report")


def test_send_email_builds_and_sends_message():
    cfg = NotifyConfig(smtp_host="smtp.test", smtp_port=587, smtp_user="u",
                       smtp_password="p", mail_from="a@test", mail_to="b@test",
                       smtp_starttls=True)
    notify.send_email(cfg, format_report([], QUEUE), smtp_factory=_SMTP)
    assert _SMTP.last.started is True          # starttls used
    assert _SMTP.last.logged == ("u", "p")     # authenticated
    assert _SMTP.last.sent["To"] == "b@test"
    assert "drifted" in _SMTP.last.sent["Subject"]


def test_notify_github_comments_on_open_pr():
    op = _Opener(get_body=lambda req: json.dumps([{"number": 7}]).encode()
                 if req.get_method() == "GET" else b"")
    did = notify.notify_github("annatchijova/koine", "feature", "abc123",
                               format_report([], QUEUE), "tok", opener=op)
    assert did == "pr#7"
    posts = [c for c in op.calls if c.get_method() == "POST"]
    assert posts and "/issues/7/comments" in posts[0].full_url


def test_notify_github_falls_back_to_commit_comment():
    op = _Opener(get_body=lambda req: b"[]")  # no open PR
    did = notify.notify_github("annatchijova/koine", "feature", "abc1234",
                               format_report([], QUEUE), "tok", opener=op)
    assert did.startswith("commit abc1234"[:13])
    posts = [c for c in op.calls if c.get_method() == "POST"]
    assert posts and "/commits/abc1234/comments" in posts[0].full_url


# ---------- dispatch: honest degradation ----------

def test_unconfigured_channels_are_skipped_not_failed():
    out = dispatch(format_report([], QUEUE), NotifyConfig())
    assert out == {"slack": "skipped", "email": "skipped", "github": "skipped"}


def test_one_channel_failing_does_not_stop_the_others_and_never_leaks_secret():
    cfg = NotifyConfig(slack_url="https://hooks.slack.com/services/SUPER-SECRET-TOKEN",
                       github_token="tok")
    ok = _Opener(get_body=lambda req: b"[]")  # github: no PR -> commit comment
    out = dispatch(format_report([], QUEUE), cfg,
                   context={"repo": "a/b", "branch": "main", "sha": "deadbee"},
                   slack_opener=_Opener(fail=True), gh_opener=ok)
    assert out["slack"].startswith("failed:")
    assert "SECRET" not in out["slack"]          # no secret leaks into the status
    assert out["github"].startswith("sent")       # other channel still ran
    assert out["email"] == "skipped"


def test_dispatch_never_raises_on_a_broken_channel():
    cfg = NotifyConfig(slack_url="https://x")
    # must not propagate the ConnectionError
    out = dispatch(format_report([], QUEUE), cfg, slack_opener=_Opener(fail=True))
    assert out["slack"].startswith("failed:")


# ---------- maybe_notify gating ----------

def test_no_notification_when_queue_is_empty():
    cfg = NotifyConfig(slack_url="https://x")
    assert maybe_notify({"work_queue": [], "changed_paths": []}, cfg) == {}


def test_no_notification_when_nothing_configured():
    result = {"work_queue": QUEUE, "changed_paths": ["README.md"], "context": {}}
    assert maybe_notify(result, NotifyConfig()) == {}
    assert maybe_notify(result, None) == {}


def test_maybe_notify_fires_on_drift():
    cfg = NotifyConfig(slack_url="https://hooks.slack.test/x")
    op = _Opener()
    result = {"work_queue": QUEUE, "changed_paths": ["README.md"],
              "context": {"repo": "a/b", "branch": "main", "sha": "x"}}
    out = maybe_notify(result, cfg, slack_opener=op)
    assert out["slack"] == "sent"
    assert op.calls  # a request really went out (to the fake)


# ---------- config from env ----------

def test_config_from_env_reads_channels():
    env = {
        "KOINE_SLACK_WEBHOOK_URL": "https://s",
        "KOINE_GITHUB_TOKEN": "t",
        "KOINE_SMTP_HOST": "smtp", "KOINE_SMTP_PORT": "465",
        "KOINE_MAIL_FROM": "a", "KOINE_MAIL_TO": "b", "KOINE_SMTP_SSL": "true",
    }
    cfg = NotifyConfig.from_env(env)
    assert cfg.any()
    assert cfg.email_ready()
    assert cfg.smtp_port == 465 and cfg.smtp_ssl is True


def test_config_empty_env_is_inert():
    cfg = NotifyConfig.from_env({})
    assert not cfg.any()
