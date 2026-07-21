"""AI Studio Spokesman — the stakeholder communication service.

Implements the WhatsApp channel of the Spokesman described in ADR-0006:
classify studio outputs into approve / inform / alarm tiers, push the
interrupting ones to the stakeholder over the Meta WhatsApp Business Cloud API,
batch the rest into a digest, and route inbound replies back into ``state/``
(ADR-0007). All secrets/config come from the environment (ADR-0011).
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
