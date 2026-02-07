from enum import Enum

class ARKState(str, Enum):
    """
    ARK lifecycle states.
    
    reserved: ID generated, waiting for metadata
    draft: Metadata provided, ready for blockchain/IPFS persistence
    published: Persisted on-chain and IPFS (internal state)
    tombstone: Deactivated by client
    """
    RESERVED = "R"
    DRAFT = "D"
    PUBLISHED = "P"
    TOMBSTONE = "T"
