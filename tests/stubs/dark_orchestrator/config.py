"""Test stub config objects for dark_orchestrator."""


class DARKConfig:
    """Simple value container matching production constructor signature."""

    def __init__(
        self,
        rpc_url: str,
        chain_id: int,
        authority_address: str,
        dark_address: str,
        admin_private_key: str,
    ) -> None:
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.authority_address = authority_address
        self.dark_address = dark_address
        self.admin_private_key = admin_private_key

