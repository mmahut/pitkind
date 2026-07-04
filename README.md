<img src=".github/assets/pitkind.svg" alt="Pitkin" width="120" align="right">
<br><br>

# Pitkin: Agentic Deliberation Orchestrator

Pitkin is an autonomous Constitutional Committee deliberation platform for Cardano
governance actions. 

## Workflow

```text
Governance action retrieval → frozen proposal and evidence snapshot → R0 → R1 → R2 → ballot
```

Retrieval happens before judging. Judging reads only the supplied snapshot and
constitution; it does not browse, call MCP, or refresh chain state.

- **R0**: each model independently identifies constitutional arguments.
- **R1/R2**: each model answers every earlier argument, gives its strongest
  counterargument, and explains why its position changed or stayed the same.
- **Ballot**: YES or NO needs a strict majority of all seats. Otherwise the
  autonomous ballot is ABSTAIN.
- **ABSTAIN** requires a specific decisive missing fact and both possible
  consequences. An established violation requires NO.

Every model receives the same constitution, proposal bytes, evidence, and
generated strict JSON schema. The output records exact prompts, model outputs,
hashes, timestamps, argument IDs, and aggregation flags. 

The package and CLI are named `pitkind`:

```bash
$ pitkind --help
```

## Install a prebuilt `pitkind`

Replace `amd64` with your architecture: `amd64`, `arm64`, or `armv7`.

```bash
curl -fLo pitkind https://github.com/mmahut/pitkind/releases/latest/download/pitkind-linux-amd64
chmod +x pitkind && sudo mv pitkind /usr/local/bin/
```

## Retrieve a governance action

The downloader records compact account, transaction, proposal, and relevant chain-state evidence.

```bash
$ pitkind snapshot gov_action174lclj6wswk3km6chl755vp24ja44yy8fjput7z20795hdpuax7qq67pvcp
```

The resulting JSON is an immutable input artifact, with a deterministic semantic
SHA-256 in the adjacent `.sha256` file. Refresh it before a run when current chain
state matters; do not overwrite an existing snapshot.

## Run a mock

Mock mode exercises all three rounds and validation without model API calls:

```bash
$ pitkind run \
  --constitution inputs/constitutions/latest \
  --system-prompt inputs/prompts/v0.1.txt \
  --deliberation-prompt-template inputs/templates/v0.1.txt \
  --proposal path/to/proposal.json \
  --config config.yaml \
  --output-dir output \
  --mock
```

## Run real deliberation

```bash
$ export OPENROUTER_API_KEY="sk-or-..."
$ export OPENAI_API_KEY="sk-..."              # seats with provider: openai
$ export ANTHROPIC_API_KEY="sk-ant-..."       # seats with provider: anthropic
$ pitkind run \
  --constitution inputs/constitutions/latest \
  --system-prompt inputs/prompts/v0.1.txt \
  --deliberation-prompt-template inputs/templates/v0.1.txt \
  --proposal path/to/proposal.json \
  --config config.yaml \
  --output-dir output
```
