"""Invite chores a human does by hand. Every command only reports unless --apply.

    uv run python scripts/invites_admin.py migrate                                     # what copying would do
    uv run python scripts/invites_admin.py migrate --apply --expect-account <id>       # copy it into kw-bridge
    uv run python scripts/invites_admin.py revoke --hash <sha256>                      # what withdrawing would do
    uv run python scripts/invites_admin.py revoke --hash <sha256> --apply --expect-account <id>

Every run prints the AWS account and the table ARN first. --apply also needs --expect-account with that
same account id, so a profile pointing at another account never writes there.

migrate refuses an empty or missing SSM list (nothing to copy, and the recount must not reset the total).
It makes every hash on the hand-made SSM list (/kw/mcp/tokens) an INVITE#<hash> item in kw-bridge
with issued_to "manual" and source "manual", so DynamoDB alone can tell whether an invite is alive.
An item that already has an owner is left as it is, and only issued_to, issued_at and source are ever
set: the bridge's PC count on the item (devices) stays as it was. Running it again changes nothing.
It also sets the running total (DINVITES#total: n, revoked) from the INVITE# items that have an owner:
one Scan of kw-bridge, here on the owner's machine, so the Lambdas never need one. Run it when nobody
is taking an invite in Discord; running it again recounts.

Switching invites to DynamoDB only (no more SSM list):
1. `migrate`, check the counts, then `migrate --apply`.
2. Deploy with `-c invitesSsm=0`: the Bridge and PublicMcp functions get KW_INVITES_SSM=0 (invites.py
   stops reading the list) and lose their read grant on /kw/mcp/tokens. Hand-made invites keep working
   through their INVITE# items. To go back, deploy without the flag.
3. A week later, with the owner's approval: `aws ssm delete-parameter --region ap-northeast-2
   --name /kw/mcp/tokens`, and put "invitesSsm": "0" into infra/cdk.json's context so no later deploy
   can drop the flag. Until step 2 the list still decides for a migrated invite (taking a hash off the
   list withdraws it, as before); once the list is deleted, a deploy WITHOUT the flag cuts every
   hand-made invite at once.

revoke withdraws one invite by its hash (`revoke`, the same as /초대회수 does per invite): after step 2
this is how a hand-made invite is withdrawn. Before step 2, a hand-made invite still on the SSM list
stays alive until it is taken off the list too; the command says so.

The AWS profile needs ssm:GetParameter on /kw/mcp/tokens and, for --apply, dynamodb:Scan and
dynamodb:UpdateItem on kw-bridge (sts:GetCallerIdentity needs no grant). No token is printed, and no
hash other than the one given.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

import boto3
from boto3.dynamodb.conditions import Attr

from kwcampus import invites

REGION = "ap-northeast-2"
_HASH = re.compile(r"[0-9a-f]{64}")


class Refused(Exception):
    """Nothing was written; the message says why."""


def ssm_hashes(ssm) -> list[str]:
    """The hand-made list; [] when the parameter is gone (already switched over)."""
    try:
        value = ssm.get_parameter(Name=invites.TOKENS_PARAM)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return []
    return [str(h) for h in json.loads(value or "[]")]


def count_owned(t) -> dict[str, int]:
    """INVITE# items with an owner, and how many of them are withdrawn (one paginated Scan)."""
    kwargs = {"FilterExpression": Attr("PK").begins_with("INVITE#") & Attr("issued_to").exists(),
              "ProjectionExpression": "PK, revoked"}
    n = revoked = 0
    while True:
        page = t.scan(**kwargs)
        for item in page["Items"]:
            n += 1
            revoked += bool(item.get("revoked"))
        if "LastEvaluatedKey" not in page:
            return {"n": n, "revoked": revoked}
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def migrate(t, hashes: list[str], apply: bool, now: int | None = None) -> dict[str, int]:
    """Counts: listed, malformed, already (an owner was there), copied (or would be, without apply)."""
    if not hashes:
        raise Refused(f"{invites.TOKENS_PARAM} is empty or missing: nothing to copy, nothing written")
    now = int(now if now is not None else time.time())
    counts = {"listed": len(hashes), "malformed": 0, "already": 0, "copied": 0}
    for code_hash in dict.fromkeys(hashes):
        if not _HASH.fullmatch(code_hash):
            counts["malformed"] += 1
            continue
        item = t.get_item(Key={"PK": f"INVITE#{code_hash}", "SK": "META"}, ConsistentRead=True).get("Item") or {}
        if item.get("issued_to"):
            counts["already"] += 1
        elif not apply or invites.add_manual(t, code_hash, now):
            counts["copied"] += 1
        else:  # someone gave it an owner between the read and the write
            counts["already"] += 1
    total = count_owned(t)
    if not apply:
        total["n"] += counts["copied"]  # what the count will be once they're copied
    counts["total"], counts["total_revoked"] = total["n"], total["revoked"]
    if apply:
        t.update_item(Key={"PK": invites.TOTAL_PK, "SK": "META"}, UpdateExpression="SET n = :n, revoked = :r",
                      ExpressionAttributeValues={":n": total["n"], ":r": total["revoked"]})
    return counts


def revoke(t, code_hash: str, listed: list[str], apply: bool) -> list[str]:
    """Withdraw one invite by its hash (invites.revoke); the lines to print."""
    if not _HASH.fullmatch(code_hash):
        raise Refused("--hash must be 64 lowercase hex characters (the invite token's SHA-256)")
    item = t.get_item(Key={"PK": f"INVITE#{code_hash}", "SK": "META"}, ConsistentRead=True).get("Item") or {}
    if not item.get("issued_to"):
        raise Refused("no invite with that hash in kw-bridge (a hand-made one not migrated yet: run migrate first)")
    lines = [f"invite: source={item.get('source') or '?'} revoked={'yes' if item.get('revoked') else 'no'}"]
    if item.get("revoked"):
        lines.append("already withdrawn: nothing to do")
    elif apply:
        lines.append("withdrawn" if invites.revoke(t, code_hash) else "already withdrawn: nothing to do")
    else:
        lines.append("would withdraw it (add --apply)")
    if code_hash in listed:
        lines.append(f"still on {invites.TOKENS_PARAM}: until invitesSsm=0 is deployed that list keeps it alive. "
                     "Take it off the list too.")
    return lines


def account_of(sts) -> str:
    return str(sts.get_caller_identity()["Account"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--region", default=REGION)
    sub = parser.add_subparsers(dest="command", required=True)
    mig = sub.add_parser("migrate", help="copy the SSM invite list into kw-bridge INVITE# items")
    rev = sub.add_parser("revoke", help="withdraw one invite by its SHA-256")
    rev.add_argument("--hash", required=True, dest="code_hash")
    for command in (mig, rev):
        command.add_argument("--apply", action="store_true")
        command.add_argument("--expect-account", help="the AWS account id --apply may write to")
    args = parser.parse_args(argv)

    account = account_of(boto3.client("sts", region_name=args.region))
    print(f"account {account} · table arn:aws:dynamodb:{args.region}:{account}:table/{invites.BRIDGE_TABLE}")
    if args.apply and args.expect_account != account:
        print(f"refused: --apply needs --expect-account {account} (this profile's account); nothing written",
              file=sys.stderr)
        return 2
    t = boto3.resource("dynamodb", region_name=args.region).Table(invites.BRIDGE_TABLE)
    hashes = ssm_hashes(boto3.client("ssm", region_name=args.region))
    try:
        if args.command == "migrate":
            print(" ".join(f"{k}={v}" for k, v in migrate(t, hashes, args.apply).items()))
        else:
            print("\n".join(revoke(t, args.code_hash.strip().lower(), hashes, args.apply)))
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    if not args.apply:
        print("dry run: nothing written (add --apply --expect-account <id>)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
