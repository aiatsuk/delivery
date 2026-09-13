"""Guarded GitHub publication with recoverable external side effects.

GitHub operations use explicit argument arrays and origin-derived repository IDs.
The optional provider parameter is an API test seam; the production CLI does not
expose it. A fake provider can model a local fixture origin without network.

GitHub's match-head guard is atomic for the PR head. Its merge API provides no
equivalent expected-base argument. The adjacent base check reduces that race;
strict protection against a later base update requires GitHub branch protection.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unicodedata
from urllib.parse import urlsplit
import uuid

import git_ops
import run_engine as engine


SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
ATTRIBUTION = re.compile(
    r"(?im)(?:co-authored-by:|(?:generated|written|created|assisted|powered)\s+(?:by|with|using)\s+)"
    r"[^\n]{0,80}\b(?:claude|codex|grok|cursor|copilot|gpt(?:-\w+)?|gemini|openai)\b"
)
FIELDS = ("number,url,state,title,body,headRefName,headRefOid,baseRefName,baseRefOid,"
          "isCrossRepository,isDraft,reviewDecision,statusCheckRollup,mergeable,"
          "mergeStateStatus,mergedAt,mergeCommit,autoMergeRequest")
BASE_RACE = "GitHub locks the PR head, but a base update after the final check requires branch protection to prevent."


def github_repository(origin: str) -> str:
    """Accept exact github.com HTTPS/SSH origins, never a default gh repository."""
    engine.require(isinstance(origin, str) and origin and not any(c.isspace() for c in origin),
                   "github_origin", "Select one explicit github.com origin URL.")
    if origin.startswith("git@github.com:"):
        path = origin[len("git@github.com:"):]
    else:
        try:
            parsed = urlsplit(origin)
            valid = (parsed.hostname == "github.com" and not parsed.query and not parsed.fragment
                     and ((parsed.scheme == "https" and not parsed.username and not parsed.password and parsed.port in {None, 443})
                          or (parsed.scheme == "ssh" and parsed.username == "git" and not parsed.password and parsed.port in {None, 22})))
        except ValueError:
            valid = False
        engine.require(valid, "github_origin", "Only exact github.com HTTPS or git SSH origin URLs are supported.")
        path = parsed.path.removeprefix("/")
    path = path.removesuffix(".git")
    engine.require(REPO.fullmatch(path), "github_origin", "The origin must name exactly one GitHub owner and repository.")
    return path


class GitHub:
    """Bounded gh transport; no shell, automatic push, fork, admin bypass, or branch deletion."""

    def __init__(self, cwd):
        self.cwd = str(cwd)

    @staticmethod
    def repository(origin):
        return github_repository(origin)

    def _run(self, *arguments, json_result=False):
        environment = dict(os.environ)
        environment.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1", GH_PAGER="cat", NO_COLOR="1")
        try:
            result = subprocess.run(["gh", *arguments], cwd=self.cwd, env=environment,
                                    text=True, capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise engine.RunError("github_unavailable", "GitHub access failed or timed out; inspect the saved operation before retrying.") from exc
        engine.require(result.returncode == 0, "github_failed", "The explicit GitHub operation failed; no success was assumed.")
        engine.require(len(result.stdout) <= 2 * 1024 * 1024, "github_response_limit", "GitHub returned too much data to verify safely.")
        if not json_result:
            return result.stdout.strip()
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise engine.RunError("github_response", "GitHub did not return the requested structured result.") from exc

    def view(self, repo, number):
        return self._run("pr", "view", str(number), "--repo", f"github.com/{repo}", "--json", FIELDS, json_result=True)

    def find(self, repo, branch):
        rows = self._run("pr", "list", "--repo", f"github.com/{repo}", "--state", "all", "--base", "main",
                         "--head", branch, "--limit", "100", "--json", "number", json_result=True)
        engine.require(isinstance(rows, list) and len(rows) < 100, "ambiguous_pr", "The PR search was incomplete or malformed.")
        return [self.view(repo, row["number"]) for row in rows]

    def _body_command(self, arguments, body):
        with tempfile.TemporaryDirectory(prefix="delivery-pr-") as folder:
            path = Path(folder) / "body.md"
            path.write_text(body, encoding="utf-8")
            return self._run(*arguments, "--body-file", str(path))

    def create(self, repo, branch, title, body):
        url = self._body_command(("pr", "create", "--repo", f"github.com/{repo}", "--base", "main",
                                  "--head", branch, "--title", title), body)
        match = re.fullmatch(r"https://github\.com/" + re.escape(repo) + r"/pull/([1-9][0-9]*)", url, re.IGNORECASE)
        engine.require(match, "github_response", "PR creation returned an unexpected URL; recover using the saved ownership marker.")
        return self.view(repo, int(match.group(1)))

    def update(self, repo, number, title, body):
        self._body_command(("pr", "edit", str(number), "--repo", f"github.com/{repo}", "--title", title), body)
        return self.view(repo, number)

    def merge(self, repo, number, head):
        self._run("pr", "merge", str(number), "--repo", f"github.com/{repo}", "--rebase", "--match-head-commit", head)


def _history(value, label, limit):
    value = engine.text(value, label)
    engine.require(len(value) <= limit and "\x00" not in value and "\r" not in value,
                   "history_policy", f"{label} must be bounded English change prose.")
    engine.require(not ATTRIBUTION.search(value), "history_policy", "Describe the change without tool attribution.")
    letters = [c for c in value if c.isalpha()]
    latin = [c for c in letters if "LATIN" in unicodedata.name(c, "")]
    engine.require(letters and len(latin) / len(letters) >= 0.75, "history_language", "Use English change descriptions.")
    return value


def _checkpoint(run):
    """Persist an intent/receipt while the enclosing run transaction owns the lock.

    A checkpoint consumes a revision even when a later refusal escapes the outer
    transaction, so readers cannot reuse an optimistic revision from before it.
    """
    run["revision"] += 1
    run["updated_at"] = engine.now()
    run["history"].append({"revision": run["revision"], "at": run["updated_at"], "event": "publication_checkpoint", "state": run["state"]})
    engine.atomic_json(Path(run["root"]) / "run.json", run)


def _owned(run, owner, task_id=None):
    engine.require(owner, "missing_worktree", "The owned integration worktree is missing.")
    engine.owned_worktree_record(run, owner, task_id=task_id)
    info = git_ops.inspect_repo(owner["path"])
    engine.require(info["primary"] == run["primary"] and info["worktree"] == owner["path"]
                   and info["branch"] == owner["branch"], "worktree_identity", "The worktree repository, path, or branch changed.")
    engine.require(not info["in_progress"], "git_in_progress", "Preserve and finish the existing Git operation before continuing.")
    return info


def _validated(run):
    engine.assert_plan(run)
    engine.assert_spec(run)
    engine.require(not run.get("spec_refresh_required"), "spec_refresh_required", "Capture current rich verification snapshots and finish specification verification first.")
    snap = engine.snapshot(run)
    accepted = run.get("validated") or {}
    engine.require(accepted.get("plan_hash") == run["plan_hash"] and accepted.get("content") == snap["content"]
                   and accepted.get("base_sha") == snap["base_sha"] and accepted.get("files") == snap["files"],
                   "stale_validation", "Validate the current integrated content and base before publication.")
    engine.require(engine.gates_current(run), "verification_incomplete", "Integrated gates must pass on the current content.")
    if run["plan"]["classification"]["level"] != "small":
        engine.require(engine.spec_command(run["root"], ["status"])["state"] == "DONE",
                       "spec_verification_incomplete", "Finish the guarded rich specification verification before publication.")
        engine.require(engine.evidence_current(run.get("rich_snapshot") or {}, snap),
                       "stale_rich_snapshot", "Rich verification snapshots must match the current content and base.")
        engine.assert_rich_checks(run)
    reviews = [receipt for receipt in run["reviews"] if receipt["task"] is None]
    engine.require(reviews and reviews[-1]["verdict"] == "PASS" and engine.evidence_current(reviews[-1], snap),
                   "review_required", "The current integrated diff needs an independent passing review.")
    for task in run["plan"]["tasks"]:
        engine.verify_task_current(run, task["id"])
    scopes = [path for task in run["plan"]["tasks"] for path in task["paths"]]
    engine.require(snap["files"] and all(engine.covered(path, scopes) for path in snap["files"]),
                   "unexpected_surface", "The validated diff must contain only approved files.")
    return snap


def _committed(run):
    info = _owned(run, run["integration"])
    record = run.get("commit") or {}
    engine.require(record.get("head") == info["head"] and info["status"]["clean"], "uncommitted_head", "Commit the exact validated integration branch and preserve its HEAD before publication.")
    return info


def _origin(run, service, path=None):
    path = Path(path or run["integration"]["path"])
    fetch = git_ops._text(path, "remote", "get-url", "--all", "origin").splitlines()
    push = git_ops._text(path, "remote", "get-url", "--push", "--all", "origin").splitlines()
    engine.require(len(fetch) == len(push) == 1, "github_origin", "Origin must have one exact fetch and push destination.")
    repo = service.repository(fetch[0])
    engine.require(REPO.fullmatch(repo) and service.repository(push[0]).lower() == repo.lower(),
                   "github_origin", "Origin fetch and push URLs must identify the same repository.")
    previous = run.get("publication") or run.get("pr")
    if previous:
        engine.require(previous["repo"].lower() == repo.lower(), "origin_changed", "The origin repository changed after publication was prepared.")
    return repo


def _fresh_base(run):
    current = git_ops.main_snapshot(run["primary"])
    engine.require(current["base_sha"] == run["integration"]["base_sha"], "stale_base", "Main advanced; refresh the integration branch and rerun invalidated checks before publication or merge.")
    return current["base_sha"]


def _marker(run):
    return f"<!-- delivery-run:v1 {run['publication']['owner']} -->"


def _pr_identity(run, pr, *, head=None, base=None, number=None):
    repo = run["publication"]["repo"]
    engine.require(isinstance(pr, dict) and type(pr.get("number")) is int and pr["number"] > 0,
                   "pr_identity", "GitHub did not return an exact PR identity.")
    expected_url = f"https://github.com/{repo}/pull/{pr['number']}"
    engine.require(pr.get("url", "").lower() == expected_url.lower() and pr.get("headRefName") == run["integration"]["branch"]
                   and pr.get("baseRefName") == "main" and pr.get("isCrossRepository") is False
                   and _marker(run) in pr.get("body", ""), "pr_ownership", "This PR does not match the run's repository, branch, base, and ownership marker.")
    if number is not None:
        engine.require(pr["number"] == number, "pr_identity", "The provider returned a different PR number.")
    if head is not None:
        engine.require(pr.get("headRefOid") == head, "pr_head_changed", "The PR head differs from the exact tested commit.")
    if base is not None:
        engine.require(pr.get("baseRefOid") == base, "stale_base", "The PR base differs from the verified main snapshot.")


def _invalidate(run, reason="The integration changed; repeat its content-bound verification."):
    engine.invalidate_integration(run, reason)


def _recovery(run, operation, error):
    run["recovery"] = {"operation": operation, "code": getattr(error, "code", "operation_failed"),
                       "path": run["integration"]["path"], "branch": run["integration"]["branch"], "at": engine.now()}


def _recover_exact_commit(path, intent):
    """Recognize only a clean, equivalent single-parent commit with saved intent."""
    if not intent:
        return None
    info = git_ops.inspect_repo(path)
    if not info["status"]["clean"] or info["in_progress"] or info["head"] == intent["head"]:
        return None
    parents = git_ops._text(Path(path), "rev-list", "--parents", "-n", "1", info["head"]).split()
    if parents != [info["head"], intent["head"]]:
        return None
    if git_ops._text(Path(path), "show", "-s", "--format=%B", info["head"]) != intent["message"]:
        return None
    inventory = git_ops.inventory(path, intent["base_sha"])
    if inventory["content_fingerprint"] != intent["content"] or inventory["files"] != intent["files"]:
        return None
    return {"head": info["head"], "previous_head": intent["head"], "base_sha": intent["base_sha"],
            "kind": "recovered-equivalent-commit", "at": engine.now()}


def commit(root, message, expected_revision=None):
    message = _history(message, "Commit message", 10_000)
    failure = None
    with engine.transaction(root, "integration_committed", expected_revision) as run:
        engine.assert_gates_idle(run)
        engine.require(run["state"] in {"READY_TO_PUBLISH", "PR_OPEN"}, "commit_state", "Commit only after release readiness is verified.")
        engine.need_authority(run, "implement")
        snap = _validated(run)
        info = _owned(run, run["integration"])
        if run.get("commit") and run["commit"]["head"] == info["head"] and info["status"]["clean"]:
            return run
        recovered = _recover_exact_commit(info["worktree"], run.get("commit_intent"))
        if recovered:
            run["commit"] = recovered
            run["validated"]["head"] = recovered["head"]
            run["recovery"] = None
            return run
        engine.require(info["head"] == run["validated"]["head"], "head_changed", "The integration HEAD changed after validation.")
        inventory = git_ops.inventory(run["integration"]["path"], snap["base_sha"])
        engine.require(inventory["files"] == snap["files"], "inventory_changed", "The exact commit inventory changed.")
        run["commit_intent"] = {"head": info["head"], "content": snap["content"], "files": snap["files"],
                                "base_sha": snap["base_sha"], "message": message, "at": engine.now()}
        _checkpoint(run)
        try:
            result = git_ops.commit_changes(run["integration"]["path"], inventory["files"], message)
            run["commit"] = {"head": result["head"], "previous_head": result["previous_head"], "base_sha": snap["base_sha"], "at": engine.now()}
            after = engine.snapshot(run)
            engine.require(after["content"] == snap["content"] and after["files"] == snap["files"]
                           and result["status"]["clean"], "commit_changed", "The commit changed validated content; repeat verification on the actual tree.")
            run["validated"]["head"] = result["head"]
            run["recovery"] = None
        except (engine.RunError, git_ops.GitError, OSError) as error:
            recovered = _recover_exact_commit(info["worktree"], run["commit_intent"])
            if recovered:
                run["commit"] = recovered
                run["validated"]["head"] = recovered["head"]
                run["recovery"] = None
            else:
                _invalidate(run)
                run["state"] = "VERIFYING"
                _recovery(run, "commit", error)
                failure = error
    if failure:
        raise failure
    return engine.load(root)


def publish(root, title, body, expected_revision=None, *, provider=None):
    title, body = _history(title, "PR title", 250), _history(body, "PR body", 60_000)
    engine.require("\n" not in title, "history_policy", "The PR title must be a single line.")
    failure = None
    with engine.transaction(root, "publication_attempted", expected_revision) as run:
        engine.assert_gates_idle(run)
        engine.require(run["state"] in {"READY_TO_PUBLISH", "PR_OPEN"}, "publication_state", "Publish only a verified, committed integration branch.")
        _validated(run)
        engine.need_authority(run, "publish")
        info = _committed(run)
        service = provider or GitHub(info["worktree"])
        repo = _origin(run, service)
        base = _fresh_base(run)
        publication = run.get("publication")
        if not publication:
            publication = {"owner": uuid.uuid4().hex, "repo": repo, "branch": info["branch"], "remote_head": None, "phase": "prepared"}
            run["publication"] = publication
        engine.require(publication["branch"] == info["branch"], "branch_changed", "The publication branch changed.")
        publication.update(head=info["head"], base_sha=base, title=title, body=body)
        _checkpoint(run)
        intended_body = body + "\n\n" + _marker(run) + "\n"
        try:
            existing = None
            if run.get("pr"):
                existing = service.view(repo, run["pr"]["number"])
                _pr_identity(run, existing, number=run["pr"]["number"])
                engine.require(existing.get("headRefOid") in {run["pr"]["head"], info["head"]}, "pr_head_changed", "The owned PR changed outside this publication attempt.")
            else:
                matches = service.find(repo, info["branch"])
                engine.require(isinstance(matches, list) and len(matches) <= 1, "ambiguous_pr", "More than one PR uses the publication branch.")
                if matches:
                    existing = matches[0]
                    _pr_identity(run, existing, head=info["head"])
            if existing:
                engine.require(existing.get("state") == "OPEN", "pr_closed", "The owned PR is not open; do not adopt or reopen it implicitly.")
            actual = git_ops._remote_head(Path(info["worktree"]), "origin", info["branch"])
            expected = publication.get("remote_head")
            if actual == info["head"] and (publication.get("push_attempted") or existing):
                publication["remote_head"] = actual
            else:
                engine.require(actual == expected, "remote_changed", "The remote branch differs from the saved exact publication lease.")
                publication["push_attempted"] = True
                _checkpoint(run)
                pushed = git_ops.push_branch(info["worktree"], expected_remote_head=expected)
                engine.require(pushed["head"] == info["head"], "head_changed", "The pushed commit differs from the tested head.")
                publication["remote_head"] = pushed["head"]
            publication["phase"] = "pushed"
            _checkpoint(run)
            _committed(run)
            _fresh_base(run)
            publication["phase"] = "pr_attempted"
            _checkpoint(run)
            if existing:
                current = service.view(repo, existing["number"])
                _pr_identity(run, current, head=info["head"], base=base, number=existing["number"])
                if current.get("title") != title or current.get("body") != intended_body:
                    current = service.update(repo, existing["number"], title, intended_body)
            else:
                current = service.create(repo, info["branch"], title, intended_body)
            _pr_identity(run, current, head=info["head"])
            engine.require(current.get("state") == "OPEN", "pr_state", "The created or updated PR is not open.")
            engine.require(current.get("title") == title and current.get("body") == intended_body,
                           "pr_description_changed", "The PR description differs from the requested publication; inspect it before retrying.")
            run["pr"] = {"number": current["number"], "url": current["url"], "head": info["head"],
                         "base_sha": base, "repo": repo, "title": title}
            publication["phase"] = "published"
            run["state"] = "PR_OPEN"
            _checkpoint(run)
            _pr_identity(run, current, base=base)
            run["recovery"] = None
        except (engine.RunError, git_ops.GitError, OSError) as error:
            _recovery(run, "publish", error)
            failure = error
    if failure:
        raise failure
    return engine.load(root)


def _merge_grant(run):
    engine.need_authority(run, "merge")
    engine.require(run.get("pr") and run["authorizations"]["merge"].get("pr") == run["pr"]["number"],
                   "merge_authorization", "Merge authority must name this exact PR and current plan.")


def refresh(root, expected_revision=None, *, provider=None):
    failure = None
    with engine.transaction(root, "publication_base_refreshed", expected_revision) as run:
        engine.assert_gates_idle(run)
        engine.assert_plan(run)
        engine.require((run.get("integration_fix") or {}).get("status") != "DISPATCHED",
                       "fix_dispatch_active", "Collect the active integration correction before refreshing its branch.")
        if run.get("pr"):
            _merge_grant(run)
        else:
            engine.need_authority(run, "publish")
        recovering = (run.get("recovery") or {}).get("operation") == "rebase"
        engine.require(run["state"] in {"READY_TO_PUBLISH", "PR_OPEN", "VERIFYING"} or (run["state"] == "BLOCKED" and recovering),
                       "refresh_state", "Refresh an open PR after merge authorization; preserve unresolved Git conflicts.")
        info = _owned(run, run["integration"])
        engine.require(info["status"]["clean"], "dirty_refresh", "Commit or resolve the integration worktree before rebasing.")
        if not recovering:
            engine.require((run.get("commit") or {}).get("head") == info["head"], "head_changed", "The committed integration HEAD changed unexpectedly.")
        else:
            target = run["recovery"].get("target_base")
            engine.require(target and git_ops._git(Path(info["worktree"]), "merge-base", "--is-ancestor", target, info["head"], check=False).returncode == 0,
                           "rebase_unresolved", "Finish the saved rebase onto its recorded target before resuming refresh.")
        service = provider or GitHub(info["worktree"])
        repo = _origin(run, service)
        if run.get("pr"):
            remote_pr = service.view(repo, run["pr"]["number"])
            _pr_identity(run, remote_pr, head=run["pr"]["head"], number=run["pr"]["number"])
            engine.require(remote_pr.get("state") == "OPEN", "pr_closed", "Refresh requires the run's open PR.")
        else:
            publication = run.get("publication")
            remote = git_ops._remote_head(Path(info["worktree"]), "origin", info["branch"])
            if publication:
                allowed_remote = {publication.get("remote_head")}
                if publication.get("push_attempted"):
                    allowed_remote.add(publication.get("head"))
                engine.require(publication["branch"] == info["branch"] and remote in allowed_remote,
                               "remote_changed", "Pre-PR refresh requires the exact saved branch publication intent.")
            else:
                engine.require(remote is None, "unowned_remote_branch", "An existing remote branch without publication ownership cannot be adopted.")
                run["publication"] = {"owner": uuid.uuid4().hex, "repo": repo, "branch": info["branch"],
                                      "remote_head": None, "head": info["head"], "base_sha": run["integration"]["base_sha"], "phase": "prepared"}
            candidates = service.find(repo, info["branch"])
            engine.require(isinstance(candidates, list) and len(candidates) <= 1, "ambiguous_pr", "The saved branch has ambiguous PR ownership.")
            if candidates:
                found = candidates[0]
                _pr_identity(run, found, head=run["publication"]["head"])
                engine.require(found.get("state") == "OPEN", "pr_closed", "The recovered owned PR is not open.")
                run["pr"] = {"number": found["number"], "url": found["url"], "head": found["headRefOid"],
                             "base_sha": run["publication"]["base_sha"], "repo": repo, "title": found["title"]}
                run["state"] = "PR_OPEN"
                _checkpoint(run)
                _merge_grant(run)
        fresh = git_ops.main_snapshot(run["primary"])["base_sha"]
        if fresh != run["integration"]["base_sha"] or recovering:
            run["recovery"] = {"operation": "rebase", "target_base": fresh, "previous_head": info["head"],
                               "path": info["worktree"], "branch": info["branch"], "at": engine.now()}
            _invalidate(run)
            _checkpoint(run)
            try:
                result = git_ops.rebase_worktree(info["worktree"])
                run["integration"]["base_sha"] = result["base_sha"]
                run["commit"] = {"head": result["head"], "previous_head": info["head"], "base_sha": result["base_sha"], "kind": "rebased", "at": engine.now()}
                if run.get("integration_fix"):
                    run.setdefault("integration_fix_history", []).append(run["integration_fix"])
                    run["integration_fix"] = None
                run["state"] = "VERIFYING"
                run["blocker"] = None
                run["recovery"] = None
            except (engine.RunError, git_ops.GitError, OSError) as error:
                run["recovery"]["code"] = getattr(error, "code", "rebase_failed")
                run["state"] = "BLOCKED"
                run["blocker"] = {"reason": "The saved rebase needs explicit conflict resolution; its worktree and operation were preserved.", "from": "PR_OPEN", "at": engine.now()}
                failure = error
    if failure:
        raise failure
    return engine.load(root)


def _checks(pr):
    engine.require(pr.get("isDraft") is False, "draft_pr", "Mark the reviewed PR ready before merging.")
    engine.require(pr.get("reviewDecision") != "CHANGES_REQUESTED", "changes_requested", "Resolve requested changes before merge.")
    engine.require(pr.get("reviewDecision") != "REVIEW_REQUIRED", "review_pending", "GitHub still requires a review.")
    engine.require(pr.get("mergeable") == "MERGEABLE" and pr.get("mergeStateStatus") in {"CLEAN", "HAS_HOOKS"},
                   "merge_blocked", "GitHub has not confirmed this PR is currently mergeable.")
    checks = pr.get("statusCheckRollup")
    engine.require(isinstance(checks, list), "checks_unavailable", "Hosted check coverage could not be determined.")
    for check in checks:
        engine.require(isinstance(check, dict), "checks_unavailable", "A hosted check result is malformed.")
        if "status" in check:
            engine.require(check["status"] == "COMPLETED", "checks_pending", "A hosted check has not completed.")
            engine.require(check.get("conclusion") in {"SUCCESS", "NEUTRAL", "SKIPPED"}, "checks_failed", "A hosted check failed or requires action.")
        elif "state" in check:
            engine.require(check["state"] not in {"PENDING", "EXPECTED"}, "checks_pending", "A hosted status is pending.")
            engine.require(check["state"] == "SUCCESS", "checks_failed", "A hosted status did not succeed.")
        else:
            raise engine.RunError("checks_unavailable", "An unknown hosted check type prevents merge.")
    return [] if checks else ["No hosted checks are configured; existing local verification and the explicit human merge grant were used."]


def _merged(run, pr):
    _pr_identity(run, pr, head=run["pr"]["head"], number=run["pr"]["number"])
    commit = (pr.get("mergeCommit") or {}).get("oid")
    engine.require(pr.get("state") == "MERGED" and pr.get("mergedAt") and isinstance(commit, str) and SHA.fullmatch(commit),
                   "merge_unconfirmed", "GitHub has not confirmed a merged time and exact merge commit; preserve the run for retry.")
    run["merge"] = {"commit": commit, "merged_at": pr["mergedAt"], "head": run["pr"]["head"],
                    "base_sha": run["pr"]["base_sha"], "repo": run["pr"]["repo"], "pr": run["pr"]["number"],
                    "confirmed_at": engine.now(), "method": "rebase", "base_race_limitation": BASE_RACE}
    run["state"] = "MERGED"
    run["recovery"] = None
    if run.get("merge_attempt"):
        run["merge_attempt"]["status"] = "confirmed"


def merge(root, expected_revision=None, *, provider=None):
    failure = None
    with engine.transaction(root, "pr_merge_checked", expected_revision) as run:
        engine.assert_gates_idle(run)
        engine.require(run["state"] == "PR_OPEN", "merge_state", "Merge only the run's verified open PR.")
        _merge_grant(run)
        _validated(run)
        info = _committed(run)
        engine.require(info["head"] == run["pr"]["head"], "unpublished_head", "Publish the verified current head before merging.")
        service = provider or GitHub(info["worktree"])
        repo = _origin(run, service)
        pr = service.view(repo, run["pr"]["number"])
        _pr_identity(run, pr, head=info["head"], number=run["pr"]["number"])
        if pr.get("state") == "MERGED":
            engine.require(run.get("merge_attempt"), "unexpected_merge", "The PR was merged outside this recorded attempt; inspect its merge provenance before cleanup.")
            _merged(run, pr)
        elif (run.get("merge_attempt") or {}).get("status") in {"submitted", "pending"}:
            engine.require(pr.get("state") == "OPEN", "pr_closed", "The pending PR closed without a confirmed merge.")
            run["merge_attempt"].update(status="pending", last_observed_at=engine.now(), observed_state="OPEN")
            run["recovery"] = {"operation": "merge", "code": "merge_pending", "at": engine.now(),
                               "message": "The existing merge has no confirmed result; no additional merge command was submitted."}
        else:
            engine.require(pr.get("state") == "OPEN", "pr_closed", "The owned PR is not open.")
            base = _fresh_base(run)
            _pr_identity(run, pr, head=info["head"], base=base)
            warnings = _checks(pr)
            run["merge_attempt"] = {"repo": repo, "pr": pr["number"], "head": info["head"], "base_sha": base,
                                    "authority": copy.deepcopy(run["authorizations"]["merge"]), "at": engine.now(),
                                    "warnings": warnings, "base_race_limitation": BASE_RACE, "status": "prepared"}
            _checkpoint(run)
            # The head is locked atomically by GitHub. This is the final remote
            # base observation immediately before the merge command.
            _committed(run)
            _fresh_base(run)
            run["merge_attempt"]["status"] = "submitted"
            _checkpoint(run)
            try:
                service.merge(repo, pr["number"], info["head"])
                _merged(run, service.view(repo, pr["number"]))
            except (engine.RunError, git_ops.GitError, OSError) as error:
                try:
                    observed = service.view(repo, pr["number"])
                    _merged(run, observed)
                except (engine.RunError, git_ops.GitError, OSError):
                    run["merge_attempt"]["status"] = "pending"
                    _recovery(run, "merge", error)
                    failure = error
    if failure:
        raise failure
    return engine.load(root)


def cleanup(root, expected_revision=None, *, provider=None):
    current = engine.load(root)
    if current["state"] == "COMPLETE":
        engine.require(expected_revision is None or expected_revision == current["revision"], "stale_revision", "Reload the completed run before retrying cleanup.")
        return current
    failure = None
    with engine.transaction(root, "merged_worktrees_cleaned", expected_revision) as run:
        engine.assert_gates_idle(run)
        engine.require(run["state"] == "MERGED" and run.get("merge"), "cleanup_state", "Confirm the merge before cleaning any worktree.")
        engine.assert_plan(run)
        _merge_grant(run)
        service = provider or GitHub(run["primary"])
        _origin(run, service, run["primary"])
        observed = service.view(run["pr"]["repo"], run["pr"]["number"])
        _pr_identity(run, observed, head=run["pr"]["head"], number=run["pr"]["number"])
        engine.require(observed.get("state") == "MERGED" and (observed.get("mergeCommit") or {}).get("oid") == run["merge"]["commit"]
                       and observed.get("mergedAt"), "merge_unconfirmed", "The exact recorded merge could not be reconfirmed.")
        primary = git_ops.prepare_primary(run["primary"])
        engine.require(git_ops._git(Path(run["primary"]), "merge-base", "--is-ancestor", run["merge"]["commit"], primary["head"], check=False).returncode == 0,
                       "merge_not_in_main", "Fresh primary main does not contain the confirmed merge commit.")
        cleanup_state = run.setdefault("cleanup", {"worktrees": {}, "preserved_prior_versions": [], "blockers": []})
        cleanup_state["primary_head"] = primary["head"]
        cleanup_state["preserved_prior_versions"] = [
            {"version": version.get("version"), "worktrees": [owner["path"] for owner in list(version.get("tasks", {}).values())
             + ([version["integration"]] if version.get("integration") else [])]}
            for version in run.get("previous_versions", run.get("prior_versions", []))]
        prior_attempts = []
        def preserve_attempts(key, owner):
            for previous in owner.get("previous_attempts", []):
                prior_attempts.append({"owner": key, "path": previous["path"], "branch": previous["branch"],
                                       "ownership_receipt": previous.get("ownership_receipt")})
                preserve_attempts(key, previous)
        for key, owner in list(run["tasks"].items()) + [("integration", run["integration"])]:
            preserve_attempts(key, owner)
        cleanup_state["preserved_prior_attempts"] = prior_attempts
        cleanup_state["blockers"] = []
        owners = [(task_id, owner) for task_id, owner in run["tasks"].items()] + [("integration", run["integration"])]
        try:
            # Verify the whole dependency graph while every task tree still
            # exists. Later exact commits/removals cannot invalidate this saved
            # cleanup evidence merely by retiring an upstream task first.
            for key, owner in owners:
                if key in cleanup_state["worktrees"]:
                    continue
                task_id = None if key == "integration" else key
                info = _owned(run, owner, task_id)
                engine.require(not info["status"]["ignored"] and not info["status"]["untracked"]
                               and not git_ops._artifact_inventory(Path(owner["path"])),
                               "cleanup_artifacts", "Preserve ignored, untracked, and empty-directory artifacts before cleanup.")
                if task_id:
                    engine.verify_task_current(run, task_id)
                    snapshot = engine.snapshot(run, task_id)
                else:
                    engine.require(info["head"] == run["merge"]["head"] and info["status"]["clean"], "cleanup_identity", "The merged integration worktree changed after merge.")
                    snapshot = engine.snapshot(run)
                cleanup_state["worktrees"][key] = {"path": owner["path"], "branch": owner["branch"], "head": info["head"],
                                                  "content": snapshot["content"], "files": snapshot["files"], "removed": False}
            _checkpoint(run)
            for key, owner in owners:
                receipt = cleanup_state["worktrees"].get(key)
                if receipt and receipt.get("removed"):
                    continue
                if receipt and not Path(owner["path"]).exists() and not Path(owner["path"]).is_symlink():
                    purpose = "integration" if key == "integration" else "task-" + key
                    creation = engine._load_worktree_receipt(run, purpose, owner.get("attempt", 0))
                    engine.require(creation["path"] == owner["path"] == receipt["path"]
                                   and creation["branch"] == owner["branch"] == receipt["branch"],
                                   "cleanup_identity", "The interrupted cleanup no longer matches its durable creation receipt.")
                    registered = git_ops._worktrees(Path(run["primary"]))
                    engine.require(not any(row.get("worktree") == owner["path"] for row in registered), "cleanup_missing", "A missing worktree still has a Git registration; preserve its ownership record.")
                    branch_head = git_ops._ref_sha(Path(run["primary"]), "refs/heads/" + owner["branch"])
                    engine.require(branch_head == receipt["head"], "cleanup_identity", "The preserved branch changed after cleanup was interrupted.")
                    receipt.update(removed=True, recovered=True, branch_preserved=True)
                    _checkpoint(run)
                    continue
                task_id = None if key == "integration" else key
                info = _owned(run, owner, task_id)
                engine.require(not info["status"]["ignored"] and not info["status"]["untracked"]
                               and not git_ops._artifact_inventory(Path(owner["path"])),
                               "cleanup_artifacts", "Preserve ignored, untracked, and empty-directory artifacts before cleanup.")
                if info["head"] != receipt["head"] and receipt.get("commit_intent"):
                    recovered = _recover_exact_commit(owner["path"], receipt["commit_intent"])
                    if recovered:
                        receipt["head"] = recovered["head"]
                        receipt["committed_reviewed_files"] = git_ops._diff_names(Path(owner["path"]), recovered["previous_head"], recovered["head"])
                        receipt["commit_recovered"] = True
                        _checkpoint(run)
                engine.require(info["head"] == receipt["head"], "cleanup_identity", "The worktree HEAD changed after cleanup was prepared.")
                before = git_ops.inventory(owner["path"], owner["base_sha"])
                engine.require(before["content_fingerprint"] == receipt["content"], "cleanup_changed", "Reviewed content changed after cleanup was prepared.")
                if not info["status"]["clean"]:
                    engine.require(task_id is not None and not info["status"]["unstaged"], "cleanup_dirty", "Only exact reviewed staged task changes may be committed during cleanup.")
                    receipt["commit_intent"] = {"head": info["head"], "base_sha": owner["base_sha"], "content": receipt["content"],
                                                "files": receipt["files"], "message": "Preserve reviewed task result"}
                    _checkpoint(run)
                    committed = git_ops.commit_changes(owner["path"], receipt["files"], "Preserve reviewed task result")
                    receipt["head"] = committed["head"]
                    receipt["committed_reviewed_files"] = committed["files"]
                    _checkpoint(run)
                    after = git_ops.inventory(owner["path"], owner["base_sha"])
                    engine.require(after["content_fingerprint"] == receipt["content"], "cleanup_changed", "The cleanup commit changed reviewed content; preserve the worktree.")
                result = git_ops.remove_worktree(run["primary"], owner["path"], receipt["branch"], receipt["head"])
                receipt.update(removed=result["removed"], branch_preserved=result["branch_preserved"], at=engine.now())
                _checkpoint(run)
            if run.get("product"):
                import product
                context = run["product"]
                work_item = (context.get("work_item") or {}).get("path")
                run["product_outcome"] = product.record_outcome(
                    context["root"], run["id"], f"Merged pull request #{run['pr']['number']}: {run['pr']['title']}.",
                    [run["pr"]["url"], str(Path(run["root"]) / "plan.json"), str(Path(run["root"]) / "run.json")],
                    "Use the linked PR and verification evidence when planning the next change.", work_item=work_item)
            run["state"] = "COMPLETE"
            run["recovery"] = None
        except (engine.RunError, git_ops.GitError, OSError) as error:
            cleanup_state["blockers"] = [{"code": getattr(error, "code", "cleanup_failed"), "message": str(error)}]
            failure = error
        except Exception as error:
            # Product memory may fail after all Git cleanup completed. Keep its
            # receipt retryable instead of claiming COMPLETE or losing progress.
            cleanup_state["blockers"] = [{"code": getattr(error, "code", "outcome_failed"), "message": "The durable outcome was not recorded; preserve the run and retry cleanup."}]
            failure = engine.RunError(cleanup_state["blockers"][0]["code"], cleanup_state["blockers"][0]["message"])
    if failure:
        raise failure
    return engine.load(root)
