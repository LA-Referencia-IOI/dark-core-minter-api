"""Test stub config objects for dark_core_lib."""


class CoreConfig:
    """Simple value container matching production constructor signature."""

    def __init__(
        self,
        rpc_url: str,
        dark_contract_address: str,
        chain_id: int = None,
        authority_contract_address: str = None,
        admin_private_key: str = None,
        read_only: bool = False,
        validate_chain_id: bool = True,
        default_gas_limit: int = 500000,
        tx_timeout_seconds: int = 120,
    ) -> None:
        self.rpc_url = rpc_url
        self.dark_contract_address = dark_contract_address
        self.chain_id = chain_id
        self.authority_contract_address = authority_contract_address
        self.admin_private_key = admin_private_key
        self.read_only = read_only
        self.validate_chain_id = validate_chain_id
        self.default_gas_limit = default_gas_limit
        self.tx_timeout_seconds = tx_timeout_seconds

