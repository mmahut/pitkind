import base64
import hashlib

import pytest

from pitkind.cli import _omit_duplicate_constitution
from pitkind.download import (
    MAX_INLINE_BYTES,
    collect_references,
    decode_cid,
    fetch_ipfs_document,
    fetch_reference_documents,
)


def _raw_cid(raw: bytes) -> str:
    digest = hashlib.sha256(raw).digest()
    header = bytes([0x01, 0x55, 0x12, 0x20])
    return "b" + base64.b32encode(header + digest).decode().lower().rstrip("=")


def test_decode_cid_variants():
    raw = decode_cid("bafkreieyuknozbtewyurfqoagvplvykadn6a4u6wglupavdz46bbsnnl6e")
    assert (raw["version"], raw["codec"], raw["hash_code"], len(raw["digest"])) == (1, 0x55, 0x12, 32)
    dag = decode_cid("bafybeidb5wgwdqqdrm72ir4bbcyfxrbx5ip3q5gs3uuxndoafqthxboe4e")
    assert (dag["version"], dag["codec"]) == (1, 0x70)
    v0 = decode_cid("QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG")
    assert (v0["version"], v0["codec"], len(v0["digest"])) == (0, 0x70, 32)
    for bad in ("zb2rhe5P4gXftAwvA4eXQ5HJwsER2owDyS9sKaQRRVQPn93bA", "bafkreieyuknozbtewyurfqoagvplvyka", "hello"):
        with pytest.raises(ValueError):
            decode_cid(bad)


def test_raw_cid_is_verified_against_content():
    raw = b'{"doc": "content"}'
    uri = "ipfs://" + _raw_cid(raw)
    result = fetch_ipfs_document(uri, fetch=lambda url: raw)
    assert result["status"] == "cid_verified"
    assert result["raw"] == raw

    tampered = fetch_ipfs_document(uri, fetch=lambda url: raw + b"!")
    assert tampered["status"] == "unavailable"
    assert "does not match" in next(iter(tampered["errors"].values()))
    assert "raw" not in tampered


def test_dag_cid_requires_two_agreeing_gateways():
    uri = "ipfs://bafybeidb5wgwdqqdrm72ir4bbcyfxrbx5ip3q5gs3uuxndoafqthxboe4e"
    agree = fetch_ipfs_document(uri, fetch=lambda url: b"same bytes")
    assert agree["status"] == "gateway_cross_checked"
    assert len(agree["gateways"]) == 2

    served = iter([b"one", b"two"])

    def disagreeing(url):
        return next(served)

    mismatch = fetch_ipfs_document(uri, fetch=disagreeing)
    assert mismatch["status"] == "gateway_mismatch"
    assert "raw" not in mismatch

    calls = []

    def only_first(url):
        calls.append(url)
        if len(calls) == 1:
            return b"lonely"
        raise ValueError("gateway down")

    single = fetch_ipfs_document(uri, fetch=only_first)
    assert single["status"] == "single_gateway_unverified"
    assert single["raw"] == b"lonely"


def test_collect_references_dedupes_and_scans_body():
    metadata_doc = {"body": {
        "references": [
            {"@type": "Other", "label": "Doc", "uri": "ipfs://bafybeidb5wgwdqqdrm72ir4bbcyfxrbx5ip3q5gs3uuxndoafqthxboe4e"},
            {"@type": "Other", "label": "Site", "uri": "https://example.org/"},
            {"@type": "Other", "label": "Dup", "uri": "https://example.org/"},
        ],
        "rationale": "See also ipfs://QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG for details.",
    }}
    references = collect_references(metadata_doc)
    assert [r["uri"] for r in references] == [
        "ipfs://bafybeidb5wgwdqqdrm72ir4bbcyfxrbx5ip3q5gs3uuxndoafqthxboe4e",
        "https://example.org/",
        "ipfs://QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG",
    ]
    assert collect_references({"body": {"title": "no refs"}}) == []


def test_duplicate_constitution_content_is_omitted_at_run_time():
    constitution = "Article I: Example."
    digest = hashlib.sha256(constitution.encode()).hexdigest()
    proposal = {"evidence": {"documents": [
        {"label": "Constitution", "sha256": digest, "content": constitution},
        {"label": "Other", "sha256": "f" * 64, "content": "keep me"},
    ]}}
    _omit_duplicate_constitution(proposal, constitution)
    duplicate, other = proposal["evidence"]["documents"]
    assert "content" not in duplicate
    assert "identical to the constitution" in duplicate["content_note"]
    assert other["content"] == "keep me"
    _omit_duplicate_constitution({}, constitution)  # tolerates absent evidence


def test_fetch_reference_documents_inlines_text_and_skips_binary(tmp_path):
    text = b'{"terms": "all of them"}'
    binary = b"%PDF-1.7\x00\xff binary"
    big = b"x" * (MAX_INLINE_BYTES + 1)
    by_cid = {_raw_cid(raw): raw for raw in (text, binary, big)}

    def fetch(url):
        return by_cid[url.rsplit("/", 1)[-1]]

    references = [
        {"label": "Terms", "uri": "ipfs://" + _raw_cid(text)},
        {"label": "Audit PDF", "uri": "ipfs://" + _raw_cid(binary)},
        {"label": "Big", "uri": "ipfs://" + _raw_cid(big)},
        {"label": "Website", "uri": "https://example.org/"},
    ]
    documents = fetch_reference_documents(references, tmp_path / "documents", fetch=fetch)

    terms, audit, bigdoc, site = documents
    assert terms["status"] == "cid_verified"
    assert terms["content"] == text.decode()
    assert (tmp_path / terms["file"]).read_bytes() == text

    assert "content" not in audit
    assert audit["content_note"].startswith("binary document")
    assert (tmp_path / audit["file"]).read_bytes() == binary

    assert "content" not in bigdoc
    assert "size budget" in bigdoc["content_note"]

    assert site["status"] == "not_retrieved"
    assert "no on-chain content hash" in site["reason"]
    assert "file" not in site
