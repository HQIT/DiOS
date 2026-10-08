"""Typed policy-service failure, distinct from a business-policy denial."""


class PolicyEvaluationError(RuntimeError):
    """An optional policy adapter could not obtain a valid decision.

    Details may contain sensitive context and must never be returned or audited.
    POST mediation records only the fixed POLICY_ENGINE_ERROR reason.
    """
