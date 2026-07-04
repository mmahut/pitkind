from __future__ import annotations

import json

_VALID_STUBS = [
    {
        "engaged_provisions": [
            {
                "provision": "Article I §3",
                "requirement": "Treasury withdrawals must include a detailed budget breakdown.",
                "assessment": "satisfied",
                "reasoning": "The proposal includes a line-item budget with justification for each expenditure.",
            }
        ],
        "decisive_provision": "Article I §3",
        "verdict": "YES",
        "confidence": "high",
        "rationale": "The proposal satisfies all assessed provisions. The budget is transparent and the scope is clearly defined within constitutional limits.",
    },
    {
        "engaged_provisions": [
            {
                "provision": "Article I §3",
                "requirement": "Treasury withdrawals must include a detailed budget breakdown.",
                "assessment": "satisfied",
                "reasoning": "Budget breakdown is present and sufficiently detailed.",
            },
            {
                "provision": "Article II §1",
                "requirement": "Proposals must not concentrate power in a single entity.",
                "assessment": "not_applicable",
                "reasoning": "This is a treasury withdrawal, not a governance restructuring.",
            },
        ],
        "decisive_provision": "Article I §3",
        "verdict": "YES",
        "confidence": "medium",
        "rationale": "Proposal meets constitutional requirements. Minor ambiguity around fund disbursement timeline does not rise to a constitutional violation.",
    },
    {
        "engaged_provisions": [
            {
                "provision": "Article I §3",
                "requirement": "Treasury withdrawals must include a detailed budget breakdown.",
                "assessment": "satisfied",
                "reasoning": "The submitted budget addresses all required categories.",
            }
        ],
        "decisive_provision": None,
        "verdict": "YES",
        "confidence": "high",
        "rationale": "All provisions assessed as satisfied. The proposal is straightforward and constitutionally compliant based on the submitted documentation.",
    },
    {
        "engaged_provisions": [],
        "decisive_provision": None,
        "verdict": "NO",
        "confidence": "low",
        "rationale": "Insufficient information to verify constitutional compliance. The proposal body lacks specificity required to assess key provisions. Rejecting on precautionary grounds.",
    },
    {
        "engaged_provisions": [
            {
                "provision": "Article I §3",
                "requirement": "Treasury withdrawals must include a detailed budget breakdown.",
                "assessment": "violated",
                "reasoning": "The budget is aggregated rather than itemised, preventing meaningful review.",
            }
        ],
        "decisive_provision": "Article I §3",
        "verdict": "NO",
        "confidence": "high",
        "rationale": "The proposal violates Article I §3 by failing to provide an itemised budget. This is a clear constitutional requirement for treasury withdrawals.",
    },
]

_INVALID_JSON = "I have reviewed the proposal and my assessment is as follows: YES, it looks fine to me."


def get_stub_response(model_index: int, call_count: int, context: dict | None = None) -> str:
    """
    model_index 3 (0-based) returns invalid JSON on first call, valid on second.
    All others always return valid JSON.
    """
    if model_index == 3 and call_count == 1:
        return _INVALID_JSON
    output = json.loads(json.dumps(_VALID_STUBS[model_index % len(_VALID_STUBS)]))
    output["summary"] = output["rationale"]
    if not output["engaged_provisions"]:
        output["engaged_provisions"] = [{
            "provision": "Article I §3", "requirement": "An itemised budget is required.",
            "assessment": "violated", "reasoning": "Mock objection: budget disclosure is inadequate.",
        }]
    for provision in output["engaged_provisions"]:
        provision["evidence"] = "Mock supplied proposal budget and Article I §3."
    output["argument_responses"] = [{
        "argument_id": key, "assessment": "unresolved",
        "reasoning": "Mock reviewers disagree about whether the budget meets the requirement.",
        "evidence": "Mock supplied proposal budget and Article I §3.",
    } for key in (context or {}).get("argument_ids", [])]
    output["strongest_counterargument"] = "The opposing budget interpretation remains disputed."
    output["revision_reason"] = "No new supplied evidence changes this mock assessment."
    return json.dumps(output)
