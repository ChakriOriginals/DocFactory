"""Verify what `terraform apply` actually built inside LocalStack.

Run after `make localstack-apply`. Every check below queries the emulator for
state Terraform claims to have created, and several of them go further and
exercise the behaviour rather than the configuration — a notification that is
configured but never fires, or a RedrivePolicy that is present but not honoured,
would pass a config-only check and fail here.

WHAT A GREEN RUN MEANS
    The resources exist with the names and ARNs the configuration asks for; the
    S3 -> SQS notification is wired and delivers; the queues quarantine a
    message after `maxReceiveCount` receives; the secrets are readable; the IAM
    roles and inline policies exist as objects; and the application's own
    INFRA_MODE=assert startup check is satisfied by Terraform-created
    infrastructure.

WHAT A GREEN RUN DOES NOT MEAN
    That the IAM policies are sufficient. LocalStack Community stores IAM
    policies but does not evaluate them: every call here succeeds with any
    policy, or none. Nothing in this file is evidence about permissions. The
    permissions are covered by the static pre-flight table in
    docs/deploy_runbook.md and, finally, by the first real `terraform apply`.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_VISIBILITY = {"ingest": 30, "parse": 10, "extract": 90}
EXPECTED_MAX_RECEIVE = 3
TENANT = "dev-tenant"


class Report:
    """Accumulates results so one failure does not hide the other twenty."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.failed = 0
        self.unsupported = 0

    def ok(self, name: str, detail: str = "") -> None:
        self.rows.append(("PASS", name, detail))

    def fail(self, name: str, detail: str) -> None:
        self.rows.append(("FAIL", name, detail))
        self.failed += 1

    def check(
        self, name: str, condition: bool, detail: str = "", *, on_fail: str | None = None
    ) -> bool:
        if condition:
            self.ok(name, detail)
        else:
            self.fail(name, on_fail if on_fail is not None else detail)
        return condition

    def unsupported_by_localstack(self, name: str, detail: str) -> None:
        """Not a failure: something the emulator cannot represent at all.

        Reported separately and loudly, because an unsupported check is an
        unverified one — it moves to the first real apply, it does not go away.
        """
        self.rows.append(("N/A ", name, detail))
        self.unsupported += 1

    def render(self) -> int:
        width = max(len(name) for _, name, _ in self.rows)
        for status, name, detail in self.rows:
            print(f"  {status}  {name.ljust(width)}  {detail}")
        print()
        print(
            f"  {len(self.rows) - self.failed - self.unsupported} passed, "
            f"{self.failed} failed, {self.unsupported} not representable in LocalStack"
        )
        return 1 if self.failed else 0


def clients(endpoint: str):
    cfg = BotoConfig(retries={"max_attempts": 3}, s3={"addressing_style": "path"})
    common = {
        "endpoint_url": endpoint,
        "region_name": "us-east-1",
        "aws_access_key_id": "test",
        "aws_secret_access_key": "test",
    }
    return (
        boto3.client("s3", config=cfg, **common),
        boto3.client("sqs", **common),
        boto3.client("secretsmanager", **common),
        boto3.client("iam", **common),
    )


# --- checks -----------------------------------------------------------------


def check_bucket(rep: Report, s3, out: dict) -> None:
    bucket = out["documents_bucket"]
    try:
        s3.head_bucket(Bucket=bucket)
        rep.ok("bucket exists", bucket)
    except Exception as exc:
        rep.fail("bucket exists", f"{bucket}: {exc}")
        return

    versioning = s3.get_bucket_versioning(Bucket=bucket).get("Status")
    rep.check("bucket versioning enabled", versioning == "Enabled", f"status={versioning}")

    try:
        rules = s3.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"][
            "Rules"
        ]
        algo = rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        rep.check("bucket encrypted at rest", algo == "AES256", f"algorithm={algo}")
    except Exception as exc:
        rep.fail("bucket encrypted at rest", str(exc))

    try:
        block = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
        rep.check(
            "public access fully blocked",
            all(block.values()),
            ", ".join(f"{k}={v}" for k, v in sorted(block.items())),
        )
    except Exception as exc:
        rep.fail("public access fully blocked", str(exc))


def check_notification(rep: Report, s3, out: dict) -> None:
    """The S3 -> SQS wiring, as configuration."""
    bucket = out["documents_bucket"]
    config = s3.get_bucket_notification_configuration(Bucket=bucket)
    queue_configs = config.get("QueueConfigurations", [])
    if not rep.check(
        "S3 notification configured",
        len(queue_configs) == 1,
        "1 queue configuration",
        on_fail=f"{len(queue_configs)} queue configuration(s), expected 1",
    ):
        return

    entry = queue_configs[0]
    rep.check(
        "notification targets the ingest queue",
        entry.get("QueueArn") == out["queue_arns"]["ingest"],
        entry.get("QueueArn", "(none)"),
    )
    rep.check(
        "notification fires on ObjectCreated",
        entry.get("Events") == ["s3:ObjectCreated:*"],
        str(entry.get("Events")),
    )
    rules = entry.get("Filter", {}).get("Key", {}).get("FilterRules", [])
    suffixes = [r["Value"] for r in rules if r["Name"].lower() == "suffix"]
    rep.check("notification filters to .pdf", suffixes == [".pdf"], str(rules))


def check_queues(rep: Report, sqs, out: dict) -> None:
    for stage, name in sorted(out["queue_names"].items()):
        dlq_name = out["dlq_names"][stage]
        try:
            url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
            dlq_url = sqs.get_queue_url(QueueName=dlq_name)["QueueUrl"]
        except Exception as exc:
            rep.fail(f"queue pair exists ({stage})", str(exc))
            continue
        rep.ok(f"queue pair exists ({stage})", f"{name} + {dlq_name}")

        attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
        dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]

        redrive = json.loads(attrs.get("RedrivePolicy", "{}"))
        rep.check(
            f"redrive policy maxReceiveCount=3 ({stage})",
            int(redrive.get("maxReceiveCount", -1)) == EXPECTED_MAX_RECEIVE,
            f"maxReceiveCount={redrive.get('maxReceiveCount')}",
        )
        rep.check(
            f"redrive targets the matching DLQ ({stage})",
            redrive.get("deadLetterTargetArn") == dlq_arn,
            redrive.get("deadLetterTargetArn", "(none)"),
        )
        rep.check(
            f"visibility timeout ({stage})",
            int(attrs.get("VisibilityTimeout", -1)) == EXPECTED_VISIBILITY[stage],
            f"{attrs.get('VisibilityTimeout')}s (expected {EXPECTED_VISIBILITY[stage]}s)",
        )
        rep.check(
            f"long polling enabled ({stage})",
            int(attrs.get("ReceiveMessageWaitTimeSeconds", 0)) == 10,
            f"{attrs.get('ReceiveMessageWaitTimeSeconds')}s",
        )


def check_ingest_queue_policy(rep: Report, sqs, out: dict) -> None:
    url = sqs.get_queue_url(QueueName=out["queue_names"]["ingest"])["QueueUrl"]
    raw = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["Policy"])["Attributes"].get(
        "Policy"
    )
    if not rep.check(
        "ingest queue has a resource policy", bool(raw), "present", on_fail="(missing)"
    ):
        return
    statement = json.loads(raw)["Statement"][0]
    rep.check(
        "policy allows s3.amazonaws.com to SendMessage",
        statement["Principal"].get("Service") == "s3.amazonaws.com"
        and statement["Action"] == "sqs:SendMessage"
        and statement["Effect"] == "Allow",
        f"{statement['Effect']} {statement['Action']} to {statement['Principal']}",
    )
    condition = statement.get("Condition", {})
    rep.check(
        "policy scoped to this bucket and account",
        condition.get("ArnEquals", {}).get("aws:SourceArn") == out["documents_bucket_arn"]
        and "aws:SourceAccount" in condition.get("StringEquals", {}),
        json.dumps(condition),
    )


def check_secrets(rep: Report, sm, out: dict) -> None:
    for key, arn in sorted(out["secret_arns"].items()):
        try:
            value = sm.get_secret_value(SecretId=arn)["SecretString"]
            rep.check(f"secret readable ({key})", bool(value), f"{len(value)} chars")
        except Exception as exc:
            rep.fail(f"secret readable ({key})", str(exc))


def check_iam(rep: Report, iam, out: dict) -> None:
    """IAM as OBJECTS. LocalStack does not enforce them; see the module docstring."""
    expected_inline = {
        "docfactory-dev-task-execution": "read-stack-secrets",
        "docfactory-dev-api-task": "api-data-plane",
        "docfactory-dev-worker-task": "worker-data-plane",
    }
    for role_name, policy_name in sorted(expected_inline.items()):
        try:
            role = iam.get_role(RoleName=role_name)["Role"]
        except Exception as exc:
            rep.fail(f"role exists ({role_name})", str(exc))
            continue
        rep.ok(f"role exists ({role_name})", role["Arn"])

        trust = role["AssumeRolePolicyDocument"]
        if isinstance(trust, str):
            trust = json.loads(trust)
        principals = trust["Statement"][0]["Principal"].get("Service")
        rep.check(
            f"trust policy is ecs-tasks only ({role_name})",
            principals == "ecs-tasks.amazonaws.com" or principals == ["ecs-tasks.amazonaws.com"],
            str(principals),
        )
        try:
            iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)
            rep.ok(f"inline policy attached ({role_name})", policy_name)
        except Exception as exc:
            rep.fail(f"inline policy attached ({role_name})", f"{policy_name}: {exc}")

    # The task roles must NOT be able to create infrastructure. This asserts the
    # absence of the grant in the policy document — which is a statement about
    # the document, not about enforcement.
    for role_name in ("docfactory-dev-api-task", "docfactory-dev-worker-task"):
        try:
            doc = iam.get_role_policy(RoleName=role_name, PolicyName=expected_inline[role_name])[
                "PolicyDocument"
            ]
        except Exception:
            continue
        if isinstance(doc, str):
            doc = json.loads(doc)
        actions = {
            action
            for statement in doc["Statement"]
            for action in (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
        }
        forbidden = actions & {"sqs:CreateQueue", "s3:CreateBucket", "sqs:*", "s3:*", "*"}
        rep.check(
            f"no infrastructure-creating grant ({role_name})",
            not forbidden,
            f"granted: {sorted(actions)}",
            on_fail=f"FOUND {sorted(forbidden)}",
        )

        # The regression guard for the 4c.5c finding. `assert_queues()` resolves
        # a URL for every DLQ at startup; without a grant on the DLQ ARNs both
        # services crash-loop on their first task start, and no local run and
        # no LocalStack run can catch it because neither enforces IAM. So it is
        # checked here as a property of the policy DOCUMENT.
        dlq_arns = set(out["dlq_names"].values())
        resolvable = set()
        for statement in doc["Statement"]:
            grants = statement["Action"]
            grants = grants if isinstance(grants, list) else [grants]
            if "sqs:GetQueueUrl" not in grants:
                continue
            resources = statement["Resource"]
            resources = resources if isinstance(resources, list) else [resources]
            resolvable |= {arn.rsplit(":", 1)[-1] for arn in resources}
        missing = dlq_arns - resolvable
        rep.check(
            f"can resolve every DLQ url ({role_name})",
            not missing,
            f"sqs:GetQueueUrl covers {len(dlq_arns)} DLQs",
            on_fail=f"NO GRANT for {sorted(missing)} — startup assert_queues() would 403",
        )


def check_notification_delivers(rep: Report, s3, sqs, out: dict) -> None:
    """Behaviour, not configuration: drop a PDF, expect a message on the queue.

    This is the check the configuration-only version cannot make. It also runs
    the delivered body through the application's own event parser, so a message
    that arrives in a shape the worker cannot read counts as a failure.
    """
    sys.path.insert(0, str(REPO_ROOT / "packages" / "core"))
    from docfactory_core.ingest import parse_s3_events

    bucket = out["documents_bucket"]
    url = sqs.get_queue_url(QueueName=out["queue_names"]["ingest"])["QueueUrl"]
    sqs.purge_queue(QueueUrl=url)
    time.sleep(1)

    key = f"{TENANT}/dropbox/invoice/localstack-probe.pdf"
    s3.put_object(
        Bucket=bucket, Key=key, Body=b"%PDF-1.4 localstack probe", ContentType="application/pdf"
    )

    deadline = time.time() + 30
    messages: list = []
    while time.time() < deadline and not messages:
        messages = sqs.receive_message(QueueUrl=url, WaitTimeSeconds=5, MaxNumberOfMessages=1).get(
            "Messages", []
        )
    if not rep.check(
        "S3 drop delivers to the ingest queue",
        bool(messages),
        f"1 message, key {key}",
        on_fail="no message within 30s",
    ):
        return

    body = json.loads(messages[0]["Body"])
    refs = parse_s3_events(body)
    rep.check(
        "delivered event parses with the worker's own parser",
        len(refs) == 1 and refs[0].key == key and refs[0].bucket == bucket,
        f"{[(r.bucket, r.key) for r in refs]}",
    )
    sqs.delete_message(QueueUrl=url, ReceiptHandle=messages[0]["ReceiptHandle"])

    # A non-.pdf drop must NOT produce an event: the suffix filter is what keeps
    # the pipeline's own text artifacts from re-triggering ingestion.
    s3.put_object(Bucket=bucket, Key=f"{TENANT}/dropbox/invoice/notes.txt", Body=b"not a pdf")
    time.sleep(3)
    stray = sqs.receive_message(QueueUrl=url, WaitTimeSeconds=2).get("Messages", [])
    rep.check(
        "non-.pdf drop is filtered out",
        not stray,
        "no event for the .txt drop",
        on_fail=f"{len(stray)} unexpected message(s)",
    )
    for message in stray:
        sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])


def check_redrive_behaviour(rep: Report, sqs, out: dict) -> None:
    """Behaviour: a message received 3 times lands in the DLQ, unaided.

    Phase 1 relied on this and Phase 4b's failure handling still does — the
    consumer counts nothing, the queue service does. Worth proving against an
    SQS implementation rather than assuming it from the RedrivePolicy JSON.
    """
    url = sqs.get_queue_url(QueueName=out["queue_names"]["parse"])["QueueUrl"]
    dlq_url = sqs.get_queue_url(QueueName=out["dlq_names"]["parse"])["QueueUrl"]
    sqs.purge_queue(QueueUrl=url)
    sqs.purge_queue(QueueUrl=dlq_url)
    time.sleep(1)

    sqs.send_message(QueueUrl=url, MessageBody=json.dumps({"probe": "poison"}))
    for _ in range(EXPECTED_MAX_RECEIVE + 1):
        received = sqs.receive_message(QueueUrl=url, WaitTimeSeconds=5, MaxNumberOfMessages=1).get(
            "Messages", []
        )
        if not received:
            break
        # Return it immediately, as a crashed consumer effectively does.
        sqs.change_message_visibility(
            QueueUrl=url, ReceiptHandle=received[0]["ReceiptHandle"], VisibilityTimeout=0
        )

    deadline = time.time() + 20
    dead: list = []
    while time.time() < deadline and not dead:
        dead = sqs.receive_message(QueueUrl=dlq_url, WaitTimeSeconds=5).get("Messages", [])
    if rep.check(
        f"message quarantined after {EXPECTED_MAX_RECEIVE} receives",
        bool(dead),
        "moved to the parse DLQ by the queue service",
        on_fail="never reached the DLQ",
    ):
        for message in dead:
            sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=message["ReceiptHandle"])


def check_app_assert_path(rep: Report, endpoint: str, out: dict) -> None:
    """The application's own INFRA_MODE=assert startup check, against this stack.

    The point of the assert mode is that a deployed task verifies the
    infrastructure it was given and refuses to start if anything is missing —
    it never creates. Running it here proves the two halves agree on names:
    Terraform builds `docfactory-dev-parse-dlq`, the app looks for
    `f"{queue}-dlq"`, and a mismatch would be a startup crash on AWS.
    """
    env = {
        **os.environ,
        "INFRA_MODE": "assert",
        "S3_ENDPOINT_URL": endpoint,
        "SQS_ENDPOINT_URL": endpoint,
        "S3_ACCESS_KEY": "test",
        "S3_SECRET_KEY": "test",
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_REGION": "us-east-1",
        "S3_BUCKET": out["documents_bucket"],
        "INGEST_QUEUE": out["queue_names"]["ingest"],
        "PARSE_QUEUE": out["queue_names"]["parse"],
        "EXTRACT_QUEUE": out["queue_names"]["extract"],
    }
    result = subprocess.run(
        ["uv", "run", "python", "-m", "docfactory_core.bootstrap"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    rep.check(
        "app INFRA_MODE=assert accepts the Terraform stack",
        result.returncode == 0,
        "clean startup check" if result.returncode == 0 else result.stderr.strip()[-300:],
    )


def check_unrepresentable(rep: Report) -> None:
    rep.unsupported_by_localstack(
        "ECR repositories + lifecycle policy",
        "LocalStack Community returns HTTP 501 for ecr:CreateRepository (pro feature)",
    )
    rep.unsupported_by_localstack(
        "CI deploy role inline policy",
        "references the ECR repository ARNs above, so it cannot be applied here",
    )
    rep.unsupported_by_localstack(
        "IAM policy ENFORCEMENT",
        "policies are stored, never evaluated — see docs/deploy_runbook.md, IAM pre-flight",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://localhost:4566")
    parser.add_argument(
        "--outputs",
        type=Path,
        required=True,
        help="`terraform output -json` from the applied data-plane layer.",
    )
    args = parser.parse_args()

    raw = json.loads(args.outputs.read_text())
    out = {key: entry["value"] for key, entry in raw.items()}

    s3, sqs, sm, iam = clients(args.endpoint)
    rep = Report()

    print(f"\nVerifying the applied data plane in LocalStack at {args.endpoint}\n")
    check_bucket(rep, s3, out)
    check_notification(rep, s3, out)
    check_queues(rep, sqs, out)
    check_ingest_queue_policy(rep, sqs, out)
    check_secrets(rep, sm, out)
    check_iam(rep, iam, out)
    check_notification_delivers(rep, s3, sqs, out)
    check_redrive_behaviour(rep, sqs, out)
    check_app_assert_path(rep, args.endpoint, out)
    check_unrepresentable(rep)

    code = rep.render()
    print(
        "\n  BOUNDARY: this run proves WIRING, not PERMISSIONS. LocalStack\n"
        "  Community does not enforce IAM, so no result above is evidence that\n"
        "  the task roles' policies are sufficient. That is what the IAM\n"
        "  pre-flight table in docs/deploy_runbook.md covers statically, and\n"
        "  what the first real `terraform apply` settles.\n"
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
