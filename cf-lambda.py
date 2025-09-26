# cf-lambda.py
from __future__ import annotations
import json
import os
import time
import urllib.request
import urllib.error
from typing import List, Tuple, Set, Dict, Optional

import boto3
from botocore.exceptions import ClientError

# ---------------- Cloudflare endpoints ----------------
CF_V4_URL = "https://www.cloudflare.com/ips-v4"
CF_V6_URL = "https://www.cloudflare.com/ips-v6"
CF_API_URL = "https://api.cloudflare.com/client/v4/ips"

UA_HEADERS_PLAIN = {
    "User-Agent": "Mozilla/5.0 (compatible; cf-prefixlist-updater/1.0)",
    "Accept": "text/plain,*/*;q=0.1",
}
UA_HEADERS_JSON = {
    "User-Agent": UA_HEADERS_PLAIN["User-Agent"],
    "Accept": "application/json",
}

HTTP_TIMEOUT = 30
DESCR_DEFAULT = "Cloudflare IP"
MAX_BATCH = 80          
PL_DESCR_TRUNC = 100  

# ---------------- Helpers ----------------
def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")

def http_get(url: str, headers: Dict[str, str], timeout: int = HTTP_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def http_post_json(url: str, payload: Dict, timeout: int = 10) -> Tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.getcode(), r.read().decode("utf-8", "ignore")

def summarize_items(items: List[str], limit: int = 20) -> str:
    if not items:
        return "—"
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", … (+{len(items)-limit} más)"

# ---------------- Session / optional assume role ----------------
def _session_with_optional_assume() -> boto3.Session:
    role_arn = os.getenv("ASSUME_ROLE_ARN")
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if role_arn:
        base = boto3.client("sts", region_name=region)
        creds = base.assume_role(RoleArn=role_arn, RoleSessionName="cfPrefixListUpdater")["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )
    return boto3.Session(region_name=region)

SESSION = _session_with_optional_assume()
STS = SESSION.client("sts")
DEFAULT_EC2 = SESSION.client("ec2")
ACCOUNT = STS.get_caller_identity()["Account"]
DEFAULT_REGION = DEFAULT_EC2.meta.region_name

def make_ec2(region: Optional[str]) -> boto3.client:
    if region and region != DEFAULT_REGION:
        return SESSION.client("ec2", region_name=region)
    return DEFAULT_EC2

# ---------------- Cloudflare fetch ----------------
def fetch_plain_lines(url: str) -> List[str]:
    raw = http_get(url, UA_HEADERS_PLAIN)
    out: List[str] = []
    for b in raw.splitlines():
        if not b or b.startswith(b"#"):
            continue
        out.append(b.decode("utf-8").strip())
    return out

def fetch_via_api() -> Tuple[List[str], List[str]]:
    raw = http_get(CF_API_URL, UA_HEADERS_JSON)
    data = json.loads(raw.decode("utf-8"))
    res = data.get("result", {})
    return res.get("ipv4_cidrs", []) or [], res.get("ipv6_cidrs", []) or []

def fetch_cloudflare_ips() -> Tuple[List[str], List[str]]:
    try:
        v4 = fetch_plain_lines(CF_V4_URL)
        v6 = fetch_plain_lines(CF_V6_URL)
        if v4 or v6:
            return v4, v6
    except (urllib.error.HTTPError, urllib.error.URLError):
        pass
    return fetch_via_api()

# ---------------- Prefix list discovery (support BOTH shapes) ----------------
def _pls(resp: Dict) -> List[Dict]:
    """Normalize responses that may use 'ManagedPrefixLists' or 'PrefixLists'."""
    return (resp.get("ManagedPrefixLists") or resp.get("PrefixLists") or [])

def _describe_managed_pls(ec2, ids: Optional[List[str]] = None, next_token: Optional[str] = None) -> Dict:
    kw: Dict = {}
    if ids: kw["PrefixListIds"] = ids
    if next_token: kw["NextToken"] = next_token
    try:
        return ec2.describe_managed_prefix_lists(**kw)
    except ClientError:
        return {"ManagedPrefixLists": []}

def _describe_prefix_pls(ec2, next_token: Optional[str] = None) -> Dict:
    kw: Dict = {}
    if next_token: kw["NextToken"] = next_token
    try:
        return ec2.describe_prefix_lists(**kw)
    except ClientError:
        return {"PrefixLists": []}

def _list_all_pls(ec2) -> List[Dict]:
    """Merge results from both APIs, dedup by PrefixListId, normalize keys."""
    seen, out = set(), []

    token = None
    while True:
        resp = _describe_managed_pls(ec2, next_token=token)
        for pl in _pls(resp):
            pid = pl.get("PrefixListId")
            if pid and pid not in seen:
                out.append(pl); seen.add(pid)
        token = resp.get("NextToken")
        if not token: break

    token = None
    while True:
        resp = _describe_prefix_pls(ec2, next_token=token)
        for pl in _pls(resp):
            pid = pl.get("PrefixListId")
            if pid and pid not in seen:
                out.append({
                    "PrefixListId": pid,
                    "PrefixListName": pl.get("PrefixListName"),
                    "MaxEntries": pl.get("MaxEntries"),
                    "OwnerId": pl.get("OwnerId"),
                    "Version": pl.get("Version"),
                    "State": pl.get("State"),
                    "PrefixListArn": pl.get("PrefixListArn"),
                })
                seen.add(pid)
        token = resp.get("NextToken")
        if not token: break

    return out

def _find_pl(ec2, prefix_list_id: Optional[str], fallback_name: Optional[str]) -> Optional[Dict]:
    if prefix_list_id:
        resp = _describe_managed_pls(ec2, ids=[prefix_list_id])
        pls = _pls(resp)
        if pls:
            return pls[0]
    for pl in _list_all_pls(ec2):
        if prefix_list_id and pl.get("PrefixListId") == prefix_list_id:
            return pl
    if fallback_name:
        for pl in _list_all_pls(ec2):
            if pl.get("PrefixListName") == fallback_name:
                return pl
    return None

def _describe_pl_with_retries(ec2, prefix_list_id: Optional[str], fallback_name: Optional[str],
                              attempts: int = 8, backoff: float = 0.5) -> Dict:
    last_seen: List[Dict] = []
    for i in range(attempts):
        pl = _find_pl(ec2, prefix_list_id, fallback_name)
        if pl:
            return pl
        last_seen = _list_all_pls(ec2)
        time.sleep(backoff * (2 ** i))
    region = ec2.meta.region_name
    preview = [
        {"Id": p.get("PrefixListId"), "Name": p.get("PrefixListName"), "Owner": p.get("OwnerId")}
        for p in last_seen[:20]
    ]
    raise RuntimeError(
        f"Prefix list not found after {attempts} attempts in acct {ACCOUNT}, region {region}. "
        f"Searched by id={prefix_list_id!r} name={fallback_name!r}. "
        f"Visible PLs sample: {json.dumps(preview)}"
    )

# ---------------- Entries & updates ----------------
def get_pl_entries(ec2, prefix_list_id: str, fallback_name: Optional[str]) -> Tuple[int, Set[str], int, str]:
    pl = _describe_pl_with_retries(ec2, prefix_list_id, fallback_name)
    version = int(pl.get("Version") or 1)
    max_entries = int(pl.get("MaxEntries") or 0)
    owner = pl.get("OwnerId") or ""

    have: Set[str] = set()
    token: Optional[str] = None
    while True:
        params = {"PrefixListId": prefix_list_id}
        if token:
            params["NextToken"] = token
        resp = ec2.get_managed_prefix_list_entries(**params)
        for e in (resp.get("Entries") or []):
            cidr = e.get("Cidr")
            if cidr:
                have.add(cidr)
        token = resp.get("NextToken")
        if not token:
            break
    return version, have, max_entries, owner

def _chunks(seq: List[Dict], n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]

def apply_delta(ec2, prefix_list_id: str, desc: str, want_list: List[str],
                fallback_name: Optional[str], account_owner: str) -> Dict:
    current_version, have, max_entries, owner = get_pl_entries(ec2, prefix_list_id, fallback_name)

    # Skip AWS-managed lists (owner != your account)
    if owner and owner != account_owner:
        return {
            "id": prefix_list_id,
            "from_version": current_version,
            "to_version": current_version,
            "added": [],
            "removed": [],
            "changed": False,
            "note": f"OWNER={owner} != {account_owner}. Lista AWS-managed; se omite modificación."
        }

    want: Set[str] = set(want_list)

    if max_entries and len(want) > max_entries:
        raise RuntimeError(
            f"Desired entries ({len(want)}) exceed MaxEntries ({max_entries}) for {prefix_list_id}. "
            f"Increase 'max_entries' in Terraform and re-apply."
        )

    to_add = sorted(want - have)
    to_remove = sorted(have - want)

    if not to_add and not to_remove:
        return {
            "id": prefix_list_id,
            "from_version": current_version,
            "to_version": current_version,
            "added": [],
            "removed": [],
            "changed": False,
            "summary": f"{prefix_list_id}: up to date ({len(have)} entries)"
        }

    adds = [{"Cidr": c, "Description": (desc or DESCR_DEFAULT)[:PL_DESCR_TRUNC]} for c in to_add]
    rems = [{"Cidr": c} for c in to_remove]

    add_batches = list(_chunks(adds, MAX_BATCH))
    rem_batches = list(_chunks(rems, MAX_BATCH))

    version = current_version

    def _modify(add_batch: Optional[List[Dict]], rem_batch: Optional[List[Dict]], version_hint: int) -> int:
        kwargs: Dict = {"PrefixListId": prefix_list_id, "CurrentVersion": version_hint}
        if add_batch:
            kwargs["AddEntries"] = add_batch
        if rem_batch:
            kwargs["RemoveEntries"] = rem_batch

        try:
            resp = ec2.modify_managed_prefix_list(**kwargs)
            return resp["PrefixList"]["Version"]
        except ClientError as e:
            msg = str(e)
            if "CurrentVersion" in msg or "version" in msg.lower():
                fresh = _describe_pl_with_retries(ec2, prefix_list_id, fallback_name)
                fresh_ver = int(fresh.get("Version") or version_hint)
                kwargs["CurrentVersion"] = fresh_ver
                resp = ec2.modify_managed_prefix_list(**kwargs)
                return resp["PrefixList"]["Version"]
            raise

    ai, ri = 0, 0
    while ai < len(add_batches) or ri < len(rem_batches):
        add_batch = add_batches[ai] if ai < len(add_batches) else None
        rem_batch = rem_batches[ri] if ri < len(rem_batches) else None
        version = _modify(add_batch, rem_batch, version)
        if ai < len(add_batches): ai += 1
        if ri < len(rem_batches): ri += 1

    return {
        "id": prefix_list_id,
        "from_version": current_version,
        "to_version": version,
        "added": to_add,
        "removed": to_remove,
        "changed": bool(to_add or to_remove),
        "summary": f"{prefix_list_id}: +{len(to_add)}/-{len(to_remove)} -> v{version}"
    }

# ---------------- Slack notify ----------------
def notify_slack(webhook: str, account: str, default_region: str, results: List[Dict], counts: Dict[str,int]) -> None:
    lines = []
    lines.append(f"*Cloudflare PrefixList Update*  —  acct `{account}`, region `{default_region}`")
    lines.append(f"CF counts: v4={counts.get('v4',0)}, v6={counts.get('v6',0)}")

    for r in results:
        if not r:
            continue
        id_ = r.get("id") or "N/A"
        from_v = r.get("from_version")
        to_v   = r.get("to_version")
        added  = r.get("added", [])
        removed = r.get("removed", [])
        changed = r.get("changed", False)
        if changed:
            lines.append(f"• `{id_}`: cambios  (+{len(added)}/-{len(removed)})")
            if added:
                lines.append(f"   + {summarize_items(added)}")
            if removed:
                lines.append(f"   - {summarize_items(removed)}")
        else:
            note = r.get("summary") or r.get("note") or "sin cambios"
            lines.append(f"• `{id_}`: {note}")

    text = "\n".join(lines)
    payload = {"text": text}
    try:
        code, body = http_post_json(webhook, payload, timeout=10)
        # opcional: no imprimir el webhook ni el body completo (para evitar exponer info)
        print(json.dumps({"slack_status": code}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"slack_error": str(e)}, ensure_ascii=False))

# ---------------- Lambda entry ----------------
def handler(event, context):
    desc = (os.getenv("DESCRIPTION") or DESCR_DEFAULT).strip()[:PL_DESCR_TRUNC]
    acct = ACCOUNT

    # Slack
    slack_notify = env_bool("SLACK_NOTIFY", False)
    slack_webhook = os.getenv("SLACK_WEBHOOK_URL")

    # PL config
    pl4_id = os.getenv("PL_V4_ID")
    pl6_id = os.getenv("PL_V6_ID")
    pl4_name = os.getenv("PL_V4_NAME")
    pl6_name = os.getenv("PL_V6_NAME")
    r4  = os.getenv("PL_V4_REGION") or DEFAULT_REGION
    r6  = os.getenv("PL_V6_REGION") or DEFAULT_REGION

    # Fetch desired Cloudflare CIDRs
    v4, v6 = fetch_cloudflare_ips()

    # Compute per-list deltas
    results: List[Dict] = []
    if pl4_id or pl4_name:
        ec2_v4 = make_ec2(r4)
        results.append(apply_delta(ec2_v4, pl4_id or "", desc, v4, pl4_name, acct))
    if pl6_id or pl6_name:
        ec2_v6 = make_ec2(r6)
        results.append(apply_delta(ec2_v6, pl6_id or "", desc, v6, pl6_name, acct))

    out = {
        "account": acct,
        "default_region": DEFAULT_REGION,
        "used_regions": {
            "v4": r4 if (pl4_id or pl4_name) else None,
            "v6": r6 if (pl6_id or pl6_name) else None
        },
        "counts": {"v4": len(v4), "v6": len(v6)},
        "result": results,
    }

    # === Only notify Slack when there were changes ===
    changed_results = [r for r in results if r and r.get("changed")]
    if slack_notify and slack_webhook and changed_results:
        notify_slack(slack_webhook, acct, DEFAULT_REGION, changed_results, out["counts"])
    else:
        if slack_notify and slack_webhook:
            print(json.dumps({"slack_skipped": "no changes"}, ensure_ascii=False))

    print(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    return out

# ---------------- Local run ----------------
if __name__ == "__main__":
    print(json.dumps(handler({}, None), indent=2))
