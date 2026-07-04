"""Retrieve a frozen Blockfrost snapshot; never called by the judging pipeline."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import time
from blockfrost import ApiError, BlockFrostApi
from requests import RequestException

from .io_paths import atomic_write_bytes, atomic_write_json, output_filename, utc_now

# Public path gateways for content-addressed retrieval; chunked-DAG CIDs are
# accepted only when two of them return byte-identical content, so adjacent
# entries are run by different operators.
IPFS_GATEWAYS = (
    "https://ipfs.io/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
    "https://dweb.link/ipfs/",
    "https://4everland.io/ipfs/",
    "https://flk-ipfs.xyz/ipfs/",
)
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_INLINE_BYTES = 200_000
MAX_TOTAL_INLINE_BYTES = 400_000
_VOLATILE_SNAPSHOT_KEYS = {"retrieved_at", "retrieval_started_at", "retrieval_completed_at"}


def snapshot_sha256(snapshot: dict) -> str:
    """Hash snapshot contents deterministically, excluding retrieval timestamps."""
    def stable(value):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items()
                    if key not in _VOLATILE_SNAPSHOT_KEYS}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    payload = json.dumps(stable(snapshot), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_RAW_CODEC = 0x55
_SHA2_256 = 0x12


def load_api_key(env_file: Path) -> str:
    key = os.environ.get("BLOCKFROST_API_KEY")
    if key:
        return key
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            name, separator, value = line.strip().removeprefix("export ").partition("=")
            if separator and name.strip() == "BLOCKFROST_API_KEY":
                parts = shlex.split(value, comments=True)
                if len(parts) == 1 and parts[0]:
                    return parts[0]
    raise ValueError("Set BLOCKFROST_API_KEY in the environment or the selected .env file")


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if offset >= len(data) or shift > 63:
            raise ValueError("Malformed varint in CID")
        byte = data[offset]
        value |= (byte & 0x7F) << shift
        offset += 1
        if not byte & 0x80:
            return value, offset
        shift += 7


def decode_cid(cid: str) -> dict:
    """Decode a CIDv0 or base32 CIDv1 into version, codec, hash code, digest."""
    if re.fullmatch(r"Qm[1-9A-HJ-NP-Za-km-z]{44}", cid):
        value = 0
        for char in cid:
            value = value * 58 + _BASE58_ALPHABET.index(char)
        try:
            data = value.to_bytes(34, "big")
        except OverflowError:
            raise ValueError(f"Malformed CIDv0: {cid}") from None
        if data[:2] != bytes([_SHA2_256, 32]):
            raise ValueError(f"Unsupported CIDv0 multihash: {cid}")
        return {"version": 0, "codec": 0x70, "hash_code": _SHA2_256, "digest": data[2:]}
    if not cid.startswith("b"):
        raise ValueError(f"Unsupported CID multibase: {cid}")
    body = cid[1:].upper()
    try:
        data = base64.b32decode(body + "=" * (-len(body) % 8))
    except ValueError:
        raise ValueError(f"Malformed base32 CID: {cid}") from None
    version, offset = _read_varint(data, 0)
    codec, offset = _read_varint(data, offset)
    hash_code, offset = _read_varint(data, offset)
    length, offset = _read_varint(data, offset)
    digest = data[offset:]
    if version != 1 or len(digest) != length or not digest:
        raise ValueError(f"Malformed CID: {cid}")
    return {"version": 1, "codec": codec, "hash_code": hash_code, "digest": digest}


def _fetch_gateway(url: str) -> bytes:
    import requests

    for attempt in range(3):
        response = requests.get(url, timeout=(10, 120), stream=True)
        if (response.status_code == 429 or response.status_code >= 500) and attempt < 2:
            response.close()
            time.sleep(2 ** (attempt + 1))
            continue
        response.raise_for_status()
        chunks = []
        size = 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_DOCUMENT_BYTES:
                raise ValueError(f"Referenced document exceeds {MAX_DOCUMENT_BYTES} bytes: {url}")
            chunks.append(chunk)
        return b"".join(chunks)
    raise ValueError(f"unreachable retry state: {url}")


def fetch_ipfs_document(uri: str, fetch=_fetch_gateway) -> dict:
    """Retrieve ipfs:// content and verify it against the CID.

    A raw sha2-256 CID is verified directly against the bytes. A chunked DAG
    CID (dag-pb) hashes its root node rather than the file, so short of
    walking the DAG the content is accepted only when two gateways agree.
    """
    match = re.fullmatch(r"ipfs://([A-Za-z0-9]+)((?:/[^?#]*)?)", uri)
    if match is None:
        return {"status": "unsupported_uri"}
    cid_str, path = match.group(1), match.group(2)
    try:
        cid = decode_cid(cid_str)
    except ValueError as exc:
        return {"status": "unsupported_cid", "error": str(exc)}
    direct = cid["codec"] == _RAW_CODEC and cid["hash_code"] == _SHA2_256 and not path
    agreeing: list[tuple[str, bytes]] = []
    errors: dict[str, str] = {}
    for gateway in IPFS_GATEWAYS:
        try:
            raw = fetch(gateway + cid_str + path)
        except (RequestException, ValueError, OSError) as exc:
            errors[gateway] = str(exc)
            continue
        if direct:
            if hashlib.sha256(raw).digest() == cid["digest"]:
                return {"status": "cid_verified", "raw": raw, "gateways": [gateway]}
            errors[gateway] = "content does not match the CID sha2-256 digest"
            continue
        agreeing.append((gateway, raw))
        if len(agreeing) == 2:
            first, second = agreeing
            if first[1] == second[1]:
                return {"status": "gateway_cross_checked", "raw": first[1],
                        "gateways": [first[0], second[0]]}
            return {"status": "gateway_mismatch",
                    "gateways": [first[0], second[0]], "errors": errors}
    if agreeing:
        gateway, raw = agreeing[0]
        return {"status": "single_gateway_unverified", "raw": raw,
                "gateways": [gateway], "errors": errors}
    return {"status": "unavailable", "errors": errors}


def collect_references(metadata_doc: dict) -> list[dict]:
    """Reference URIs from the hash-verified metadata, deduplicated in order."""
    body = metadata_doc.get("body") if isinstance(metadata_doc, dict) else None
    if not isinstance(body, dict):
        return []
    references = []
    seen = set()
    raw_references = body.get("references")
    for reference in raw_references if isinstance(raw_references, list) else []:
        if isinstance(reference, dict) and isinstance(reference.get("uri"), str):
            uri = reference["uri"].strip()
            if uri and uri not in seen:
                seen.add(uri)
                item = {"label": reference.get("label"), "uri": uri}
                if isinstance(reference.get("referenceHash"), dict):
                    item["reference_hash"] = reference["referenceHash"]
                references.append(item)
    for uri in sorted(set(re.findall(r"ipfs://[A-Za-z0-9]+(?:/[^\s\"'<>]*)?", json.dumps(body)))):
        if uri not in seen:
            seen.add(uri)
            references.append({"label": None, "uri": uri})
    return references


def fetch_reference_documents(references: list[dict], documents_dir: Path,
                              fetch=_fetch_gateway) -> list[dict]:
    inline_budget = MAX_TOTAL_INLINE_BYTES
    documents = []
    for reference in references:
        record = {"label": reference.get("label"), "uri": reference["uri"]}
        if not reference["uri"].startswith("ipfs://"):
            record["status"] = "not_retrieved"
            record["reason"] = (
                "referenceHash verification is not implemented"
                if "reference_hash" in reference
                else "no on-chain content hash binds this mutable URL"
            )
            documents.append(record)
            continue
        result = fetch_ipfs_document(reference["uri"], fetch=fetch)
        raw = result.pop("raw", None)
        record.update(result)
        if raw is not None:
            record.update(archive_document(raw, reference["uri"], documents_dir))
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                record["content_note"] = "binary document archived by hash; content not inlined"
            else:
                if len(raw) <= MAX_INLINE_BYTES and len(raw) <= inline_budget:
                    record["content"] = text
                    inline_budget -= len(raw)
                else:
                    record["content_note"] = "text document exceeds the inline size budget; archived only"
        documents.append(record)
    return documents


def archive_document(raw: bytes, url: str, directory: Path) -> dict:
    digest = hashlib.sha256(raw).hexdigest()
    path = directory / digest
    try:
        atomic_write_bytes(path, raw, overwrite=False)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise ValueError(f"Archived document is corrupt: {path}") from None
    return {"url": url, "sha256": digest, "byte_length": len(raw),
            "retrieved_at": utc_now().isoformat(), "file": f"{directory.name}/{digest}"}


def download(action_id: str, client: BlockFrostApi, network: str, *, documents_dir: Path,
             fetch_document=_fetch_gateway) -> dict:
    if not re.fullmatch(r"gov_action1[023456789acdefghjklmnpqrstuvwxyz]{20,100}", action_id):
        raise ValueError("Expected a gov_action1… governance action identifier")
    started_at = utc_now().isoformat()
    sources = []
    unavailable = []

    def get(method: str, *args, params: dict | None = None, optional: bool = False):
        source = {"sdk_method": method, "arguments": list(args), "params": params or {}}
        for attempt in range(3):
            try:
                result = getattr(client, method)(*args, return_type="json", **(params or {}))
            except ApiError as exc:
                status = exc.status_code
                if optional and status == 404:
                    sources.append({**source, "retrieved_at": utc_now().isoformat(), "status_code": status})
                    unavailable.append(source)
                    return None
                if (status != 429 and status < 500) or attempt == 2:
                    raise RuntimeError(f"Blockfrost HTTP {status}: {method}") from None
            except RequestException:
                if attempt == 2:
                    raise RuntimeError(f"Blockfrost request failed: {method}") from None
            else:
                sources.append({**source, "retrieved_at": utc_now().isoformat(), "status_code": 200})
                return result
            time.sleep(2 ** attempt)

    def pages(method: str, *args):
        result = []
        page = 1
        while True:
            batch = get(method, *args, params={"count": 100, "page": page, "order": "asc"}, optional=True)
            if batch is None:
                return None  # Missing history is not evidence of no registration.
            if not isinstance(batch, list):
                raise ValueError(f"Expected a list from Blockfrost: {method}")
            result.extend(batch)
            if len(batch) < 100:
                return result
            page += 1

    transactions = {}

    def select(data, fields):
        return {key: data[key] for key in fields if key in data}

    def transaction(tx_hash: str):
        if not re.fullmatch(r"[0-9a-f]{64}", tx_hash):
            raise ValueError("Blockfrost returned an invalid transaction hash")
        if tx_hash not in transactions:
            transactions[tx_hash] = select(get("transaction", tx_hash), ("hash", "block", "slot", "index", "block_time", "valid_contract"))
        return transactions[tx_hash]

    tip_before = get("block_latest")
    proposal = get("governance_proposal_by_gov_action_id", action_id)
    if not isinstance(proposal, dict) or proposal.get("id") != action_id:
        raise ValueError("Blockfrost returned a different or malformed proposal")
    proposal_tx = transaction(proposal["tx_hash"])
    proposal_block = get("block", proposal_tx["block"])
    metadata = get("governance_proposal_metadata_by_gov_action_id", action_id, optional=True)
    metadata_doc = {}
    anchor = {"status": "unavailable"}
    if metadata is not None:
        anchor.update({key: metadata.get(key) for key in ("url", "hash", "error")})
        if metadata.get("bytes") is not None and not metadata.get("error"):
            raw = bytes.fromhex(metadata["bytes"].removeprefix("\\x"))
            digest = hashlib.blake2b(raw, digest_size=32).hexdigest()
            if digest != metadata.get("hash"):
                raise ValueError("Proposal metadata does not match its on-chain anchor hash")
            metadata_doc = json.loads(raw)
            if not isinstance(metadata_doc, dict):
                raise ValueError("Expected a JSON object in the anchored proposal metadata")
            anchor.update(status="hash_verified", blake2b_256=digest, sha256=hashlib.sha256(raw).hexdigest())
            anchor["document"] = archive_document(raw, metadata["url"], documents_dir)

    # References come from the hash-verified metadata, so an ipfs:// CID there
    # is transitively bound to the on-chain anchor hash.
    documents = fetch_reference_documents(collect_references(metadata_doc), documents_dir,
                                          fetch=fetch_document) if metadata_doc else []

    withdrawals = []
    parameters = None
    if proposal["governance_type"] == "treasury_withdrawals":
        withdrawals = get("governance_proposal_withdrawals_by_gov_action_id", action_id)
        if not isinstance(withdrawals, list):
            raise ValueError("Expected a list of treasury withdrawals")
    elif proposal["governance_type"] == "parameter_change":
        parameters = get("governance_proposal_parameters_by_gov_action_id", action_id)["parameters"]

    addresses = {proposal["return_address"]}
    addresses.update(withdrawal["stake_address"] for withdrawal in withdrawals)
    accounts = {}
    for address in sorted(addresses):
        if not re.fullmatch(r"stake(?:_test)?1[023456789acdefghjklmnpqrstuvwxyz]+", address):
            raise ValueError("Blockfrost returned an invalid stake address")
        account = get("accounts", address, optional=True)
        if account is not None:
            account = select(account, ("registered", "active", "pool_id", "drep_id", "controlled_amount"))
        registrations = pages("account_registrations", address)
        delegations = pages("account_delegations", address)
        for registration in registrations or []:
            transaction(registration["tx_hash"])
        drep_id = account.get("drep_id") if account else None
        drep = None
        if drep_id and drep_id not in ("drep_always_abstain", "drep_always_no_confidence"):
            drep = get("governance_drep", drep_id, optional=True)
        accounts[address] = {
            "roles": (["deposit_return"] if address == proposal["return_address"] else [])
            + (["withdrawal_recipient"] if any(w["stake_address"] == address for w in withdrawals) else []),
            "account": account,
            "registrations": registrations,
            "stake_pool_delegations": delegations,
            "delegated_drep": drep,
            "administrator_custody_checks": {
                "applicability": "Article II section 7(6): applies while this account holds treasury funds pending onward disbursement; not a constitutional verdict.",
                "no_spo_delegation": account["pool_id"] is None if account and "pool_id" in account else None,
                "predefined_abstain_delegation": account["drep_id"] == "drep_always_abstain" if account and "drep_id" in account else None,
            },
        }

    context = {}
    if proposal["governance_type"] == "treasury_withdrawals":
        context["treasury_balance_lovelace"] = get("network")["supply"]["treasury"]
    elif proposal["governance_type"] in ("parameter_change", "hard_fork_initiation"):
        current = get("epoch_protocol_parameters", tip_before["epoch"])
        if parameters is not None:
            changed = {key for key, value in parameters.items() if value is not None}
            relevant = set(changed)
            for group in (
                {"max_tx_size", "max_block_size", "max_tx_ex_mem", "max_block_ex_mem", "max_tx_ex_steps", "max_block_ex_steps"},
                {"price_mem", "price_step", "max_tx_ex_mem", "max_tx_ex_steps", "max_block_ex_mem", "max_block_ex_steps"},
            ):
                if changed & group:
                    relevant.update(group)
            context["current_parameters"] = select(current, relevant)
            parameters = select(parameters, changed)
        else:
            context["current_parameters"] = select(current, ("protocol_major_ver", "protocol_minor_ver"))
    tip_after = get("block_latest")
    body = metadata_doc.get("body", {})
    return {
        "id": action_id,
        "title": body.get("title", action_id) if isinstance(body, dict) else action_id,
        "type": proposal["governance_type"],
        "on_chain": proposal,
        "body": body,
        "metadata": {key: value for key, value in metadata_doc.items() if key != "body"},
        "evidence": {
            "snapshot_version": "1.2", "provider": "Blockfrost", "network": network,
            "retrieval_started_at": started_at, "retrieval_completed_at": utc_now().isoformat(),
            "tip_before": select(tip_before, ("hash", "height", "slot", "epoch", "time")),
            "tip_after": select(tip_after, ("hash", "height", "slot", "epoch", "time")),
            "atomic_chain_snapshot": False,
            "proposal_block": select(proposal_block, ("hash", "height", "slot", "epoch", "time")),
            "metadata_anchor": anchor,
            "documents": documents,
            "withdrawals": withdrawals, "proposed_parameters": parameters,
            "accounts": accounts, "transactions": transactions,
            **context,
            "sources": sources, "unavailable_endpoints": unavailable,
            "limitations": [
                "Current-state API reads span the recorded retrieval interval; they are not pinned to one block or to proposal submission.",
                "Use registered for stake registration; active describes delegation, not registration. A 404 means unavailable, not deregistered.",
                "Registration history identifies registration/deregistration transactions; their block, slot, and transaction index are in transactions.",
                "DRep delegation is current state; stake_pool_delegations is not DRep delegation history.",
                "ipfs:// references from the anchored metadata are retrieved into documents and verified against their CIDs: raw sha2-256 CIDs directly, chunked DAG CIDs by cross-gateway byte agreement (see each document's status).",
                "Text documents are inlined in documents[].content up to a size budget; binary documents (e.g. PDFs) are archived by hash only and their content is not part of this snapshot.",
                "Other external URLs (https, git) are recorded but not retrieved: no on-chain hash binds their content. Net-change-limit accounting is not automatically retrieved.",
            ],
        },
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action_id")
    parser.add_argument("--network", choices=("mainnet", "preprod", "preview"), default="mainnet")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--out", type=Path, help="Destination JSON; existing files are never overwritten")
    args = parser.parse_args(argv)
    try:
        key = load_api_key(args.env_file)
        destination = args.out or Path("inputs") / output_filename(args.action_id, utc_now())
        if destination.exists():
            raise ValueError(f"Output already exists: {destination}")
        client = BlockFrostApi(project_id=key, base_url=f"https://cardano-{args.network}.blockfrost.io/api", api_version="v0")
        snapshot = download(args.action_id, client, args.network, documents_dir=destination.parent / "documents")
        atomic_write_json(destination, snapshot, overwrite=False)
        digest = snapshot_sha256(snapshot)
        atomic_write_bytes(destination.with_name(destination.name + ".sha256"),
                           f"{digest}  {destination.name}\n".encode(), overwrite=False)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"Download failed: {exc}\n")
    except (KeyError, TypeError, AttributeError) as exc:
        # A provider payload missing expected keys or of the wrong shape.
        parser.exit(1, f"Download failed: malformed provider response ({exc!r})\n")
    print(f"Written: {destination}")
    print(f"Snapshot SHA-256: {digest}")
    print(f"{snapshot['title']} ({snapshot['type']})")
    print(f"Metadata: {snapshot['evidence']['metadata_anchor']['status']}; account snapshots: {len(snapshot['evidence']['accounts'])}")
    for document in snapshot["evidence"].get("documents", []):
        print(f"Document: {document.get('status', 'archived')}"
              + (" (inlined)" if "content" in document else "")
              + f" — {document.get('label') or document['uri']}")


if __name__ == "__main__":
    main()
