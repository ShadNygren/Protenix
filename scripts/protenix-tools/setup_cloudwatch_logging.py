"""Attach a CloudWatch Logs handler to Python's root logger.

Activates ONLY when AWS_CLOUDWATCH_LOG_GROUP is set in the environment. If
that env var is absent the import is a no-op and the script exits 0 — safe to
call unconditionally from the entrypoint.

Required env vars:
    AWS_CLOUDWATCH_LOG_GROUP      — log group name (e.g. "protenix-training")
    AWS_ACCESS_KEY_ID             — real AWS access key (NOT CloudFlare R2)
    AWS_SECRET_ACCESS_KEY         — real AWS secret key

Optional env vars:
    AWS_CLOUDWATCH_REGION         — AWS region (default us-west-2)
    AWS_CLOUDWATCH_STREAM         — log stream name (default = $SALAD_MACHINE_ID
                                    or $HOSTNAME)
    AWS_CLOUDWATCH_LEVEL          — Python log level for the handler (default INFO)

Naming note (from project's CLAUDE.md):
  R2 / Cloudflare creds: CLOUDFLARE_R2_*
  Real AWS creds:        AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
These do NOT collide and can both be present at the same time. boto3's default
credential chain picks up AWS_*; R2 calls explicitly pass CLOUDFLARE_R2_*.

Use as either:
    1. Imported at the top of a long-running Python script:
           import setup_cloudwatch_logging as _  # configures root logger
    2. Run as a CLI for ad-hoc testing:
           python setup_cloudwatch_logging.py --test
"""
from __future__ import annotations

import logging
import os
import sys


def configure():
    """Configure root logger with a CloudWatch handler if env vars are set.

    Returns the watchtower handler instance, or None if not configured.
    """
    log_group = os.environ.get("AWS_CLOUDWATCH_LOG_GROUP")
    if not log_group:
        return None

    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        sys.stderr.write(
            "[setup_cloudwatch_logging] AWS_CLOUDWATCH_LOG_GROUP is set but "
            "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are missing — skipping. "
            "Set both (real AWS keys, not CLOUDFLARE_R2_*) to enable CloudWatch.\n"
        )
        return None

    try:
        import boto3  # type: ignore
        import watchtower  # type: ignore
    except ImportError as e:
        sys.stderr.write(
            f"[setup_cloudwatch_logging] watchtower/boto3 not installed: {e}\n"
        )
        return None

    region = os.environ.get("AWS_CLOUDWATCH_REGION", "us-west-2")
    stream_name = (
        os.environ.get("AWS_CLOUDWATCH_STREAM")
        or os.environ.get("SALAD_MACHINE_ID")
        or os.environ.get("HOSTNAME", "unknown-host")
    )
    level_name = os.environ.get("AWS_CLOUDWATCH_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    session = boto3.Session(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )
    handler = watchtower.CloudWatchLogHandler(
        log_group_name=log_group,
        log_stream_name=stream_name,
        boto3_client=session.client("logs"),
        send_interval=5,
        create_log_group=True,
    )
    handler.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    sys.stderr.write(
        f"[setup_cloudwatch_logging] streaming to "
        f"CloudWatch log_group={log_group} stream={stream_name} region={region}\n"
    )
    return handler


# Auto-configure on import (no-op if env vars absent)
_handler = configure()


def main():
    """CLI smoke test — only useful if env vars are set."""
    if _handler is None:
        print("CloudWatch logging not configured (no AWS_CLOUDWATCH_LOG_GROUP).")
        return 0
    logging.info("setup_cloudwatch_logging smoke-test: hello from %s",
                 os.environ.get("HOSTNAME", "unknown"))
    # Watchtower batches with send_interval=5 — flush before exit
    _handler.flush()
    print("Sent test log entry. Check CloudWatch Logs console.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
