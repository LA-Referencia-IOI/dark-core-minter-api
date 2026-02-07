from enum import Enum

class ARKState(str, Enum):
    """
    ARK lifecycle states.
    
    reserved: ID generated, waiting for metadata
    draft: Metadata provided, ready for blockchain/IPFS persistence
    update: New metadata/target pending on-chain update for existing ARK
    published: Persisted on-chain and IPFS (internal state)
    tombstone: Deactivated by client
    """
    RESERVED = "R"
    DRAFT = "D"
    UPDATE = "U"
    PUBLISHED = "P"
    TOMBSTONE = "T"
