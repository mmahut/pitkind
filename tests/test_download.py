import hashlib
import json
from contextlib import contextmanager
from unittest.mock import patch

from blockfrost import BlockFrostApi


def test_snapshot_cli_matches_readme(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from pitkind.cli import app
    from pitkind import download as module

    action = 'gov_action174lclj6wswk3km6chl755vp24ja44yy8fjput7z20795hdpuax7qq67pvcp'
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('BLOCKFROST_API_KEY', 'test')
    calls = []

    def fake_download(action_id, client, network, *, documents_dir):
        calls.append(action_id)
        return {'title': 'test', 'type': 'treasury_withdrawals',
                'evidence': {'metadata_anchor': {'status': 'hash_verified'}, 'accounts': {}}}

    monkeypatch.setattr(module, 'download', fake_download)
    runner = CliRunner()
    result = runner.invoke(app, ['snapshot', action])
    assert result.exit_code == 0, result.output
    dest = tmp_path / 'inputs' / f'{action}.json'
    before = dest.read_bytes()
    result = runner.invoke(app, ['snapshot', action])
    assert result.exit_code == 1
    assert 'already exists' in result.output
    assert dest.read_bytes() == before
    assert calls == [action]
    digest_file = dest.with_name(dest.name + '.sha256')
    assert digest_file.read_text().endswith(f"  {dest.name}\n")
    assert len(digest_file.read_text().split()[0]) == 64


def test_snapshot_hash_ignores_retrieval_timestamps_and_sorts_keys():
    first = {"b": 2, "a": [{"retrieved_at": "2026-01-01", "value": 1}],
             "evidence": {"retrieval_started_at": "one"}}
    second = {"evidence": {"retrieval_started_at": "two"},
              "a": [{"value": 1}], "b": 2}
    assert snapshot_sha256(first) == snapshot_sha256(second)
import httpx
import pytest


def test_malformed_provider_payload_reports_download_failure(tmp_path, monkeypatch, capsys):
    from pitkind import download as module

    monkeypatch.setenv("BLOCKFROST_API_KEY", "test")

    def broken(action_id, client, network, *, documents_dir):
        raise KeyError("tx_hash")

    monkeypatch.setattr(module, "download", broken)
    with pytest.raises(SystemExit) as excinfo:
        module.main([ACTION, "--out", str(tmp_path / "snapshot.json")])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "Download failed: malformed provider response" in err
    assert "tx_hash" in err

from pitkind.download import archive_document, download, load_api_key, snapshot_sha256
from pitkind.io_paths import atomic_write_json

ACTION = "gov_action174lclj6wswk3km6chl755vp24ja44yy8fjput7z20795hdpuax7qq67pvcp"
ADDRESS = "stake1uyl49lk2c6vfcvjq9zy0r640rf73k0qw5f64e5t7weagf3s90c7as"
BASE = "https://cardano-mainnet.blockfrost.io/api/v0"


@contextmanager
def sdk(handler):
    # Exercise real SDK endpoint methods and error conversion, without network access.
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with patch("requests.get", side_effect=transport.get):
            yield BlockFrostApi(project_id="secret-test-key", base_url=BASE.removesuffix("/v0"), api_version="v0")


def _response(request):
    path = request.url.path.removeprefix("/api/v0")
    if path.startswith("/blocks/"):
        data = {"hash": "c" * 64, "height": 1000, "slot": 2000, "epoch": 600}
    elif path == f"/governance/proposals/{ACTION}":
        data = {"id": ACTION, "tx_hash": "a" * 64, "cert_index": 0,
                "governance_type": "treasury_withdrawals", "return_address": ADDRESS}
    elif path.endswith("/metadata"):
        raw = json.dumps({"body": {"title": "Test action", "abstract": "Test text"}}).encode()
        data = {"bytes": "\\x" + raw.hex(), "hash": hashlib.blake2b(raw, digest_size=32).hexdigest(),
                "url": "https://untrusted.example/metadata.json"}
    elif path.endswith("/withdrawals"):
        data = [{"stake_address": ADDRESS, "amount": "1000000"}]
    elif path == f"/accounts/{ADDRESS}":
        data = {"registered": True, "active": False, "pool_id": None, "drep_id": None}
    elif path.endswith("/registrations"):
        row = {"tx_hash": "b" * 64, "action": "registered", "block_height": 900, "tx_slot": 1800}
        data = [row] * (100 if request.url.params["page"] == "1" else 1)
    elif path.endswith("/delegations"):
        data = []
    elif path.startswith("/txs/"):
        data = {"hash": path.split("/")[-1], "block": "c" * 64, "slot": 1900, "index": 0}
    elif path == "/network":
        data = {"supply": {"treasury": "999999999", "total": "unneeded"}}
    else:
        raise AssertionError(f"Unexpected request: {path}")
    return httpx.Response(200, json=data)


def test_snapshot_paginates_and_distinguishes_registration_from_delegation(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "cardano-mainnet.blockfrost.io"
        return _response(request)

    with sdk(handler) as client:
        snapshot = download(ACTION, client, "mainnet", documents_dir=tmp_path / "documents")
    evidence = snapshot["evidence"]
    account = evidence["accounts"][ADDRESS]
    assert account["roles"] == ["deposit_return", "withdrawal_recipient"]
    assert account["account"]["registered"] is True
    assert account["account"]["active"] is False
    assert len(account["registrations"]) == 101
    assert evidence["transactions"]["b" * 64]["block"] == "c" * 64
    assert sum(str(r.url).endswith("/txs/" + "b" * 64) for r in requests) == 1
    assert evidence["metadata_anchor"]["status"] == "hash_verified"
    assert snapshot["body"]["title"] == snapshot["title"] == "Test action"
    assert evidence["atomic_chain_snapshot"] is False
    assert "secret-test-key" not in json.dumps(snapshot)
    assert account["administrator_custody_checks"]["predefined_abstain_delegation"] is False
    assert account["administrator_custody_checks"]["no_spo_delegation"] is True
    assert evidence["treasury_balance_lovelace"] == "999999999"
    assert "protocol_parameters" not in evidence and "network_state" not in evidence
    assert not any("/epochs/" in r.url.path for r in requests)
    document = evidence["metadata_anchor"]["document"]
    assert hashlib.sha256((tmp_path / document["file"]).read_bytes()).hexdigest() == document["sha256"]


def test_metadata_hash_mismatch_stops_download(tmp_path):
    def handler(request):
        response = _response(request)
        if request.url.path.endswith("/metadata"):
            data = response.json()
            data["hash"] = "0" * 64
            return httpx.Response(200, json=data)
        return response

    with sdk(handler) as client:
        with pytest.raises(ValueError, match="anchor hash"):
            download(ACTION, client, "mainnet", documents_dir=tmp_path / "documents")


def test_missing_metadata_and_account_are_explicit_unknowns(tmp_path):
    def handler(request):
        if "/accounts/" in request.url.path or request.url.path.endswith("/metadata"):
            return httpx.Response(404, json={"error": "not found"})
        return _response(request)

    with sdk(handler) as client:
        snapshot = download(ACTION, client, "mainnet", documents_dir=tmp_path / "documents")
    account = snapshot["evidence"]["accounts"][ADDRESS]
    assert account["account"] is account["registrations"] is None
    assert snapshot["body"] == {}
    assert snapshot["evidence"]["metadata_anchor"]["status"] == "unavailable"
    assert len(snapshot["evidence"]["unavailable_endpoints"]) == 4


def test_rate_limit_is_retried_but_authentication_failure_is_not(monkeypatch, tmp_path):
    statuses = iter([429, 403])
    sleeps = []
    monkeypatch.setattr("pitkind.download.time.sleep", sleeps.append)
    with sdk(lambda r: httpx.Response(next(statuses))) as client:
        with pytest.raises(RuntimeError, match="HTTP 403"):
            download(ACTION, client, "mainnet", documents_dir=tmp_path / "documents")
    assert sleeps == [1]


def test_dotenv_is_parsed_without_executing_shell(tmp_path, monkeypatch):
    monkeypatch.delenv("BLOCKFROST_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text('export BLOCKFROST_API_KEY="$(do-not-execute)" # comment\n')
    assert load_api_key(env) == "$(do-not-execute)"
    monkeypatch.setenv("BLOCKFROST_API_KEY", "environment-key")
    assert load_api_key(env) == "environment-key"


def test_snapshot_publication_cannot_replace_existing_file(tmp_path):
    path = tmp_path / "snapshot.json"
    atomic_write_json(path, {"original": True}, overwrite=False)
    with pytest.raises(FileExistsError):
        atomic_write_json(path, {"replacement": True}, overwrite=False)
    assert json.loads(path.read_text()) == {"original": True}
    assert list(tmp_path.iterdir()) == [path]


def test_archive_preserves_exact_bytes_and_rejects_corruption(tmp_path):
    raw = b'{ "text": "original spacing" }\r\n'
    entry = archive_document(raw, "https://example.org/document", tmp_path / "documents")
    path = tmp_path / entry["file"]
    assert path.read_bytes() == raw
    archive_document(raw, entry["url"], tmp_path / "documents")
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="corrupt"):
        archive_document(raw, entry["url"], tmp_path / "documents")


def test_parameter_context_keeps_changed_values_and_related_limits_only(tmp_path):
    def handler(request):
        if request.url.path.endswith("/parameters"):
            data = ({"parameters": {"max_tx_size": 20000, "cost_models": None}}
                    if "/governance/" in request.url.path else
                    {"max_tx_size": 16000, "max_block_size": 90000, "cost_models": {"large": "unneeded"}})
            return httpx.Response(200, json=data)
        response = _response(request)
        if request.url.path.endswith(ACTION):
            data = response.json()
            data["governance_type"] = "parameter_change"
            return httpx.Response(200, json=data)
        return response

    with sdk(handler) as client:
        evidence = download(ACTION, client, "mainnet", documents_dir=tmp_path / "documents")["evidence"]
    assert evidence["proposed_parameters"] == {"max_tx_size": 20000}
    assert evidence["current_parameters"] == {"max_tx_size": 16000, "max_block_size": 90000}
    assert "treasury_balance_lovelace" not in evidence
