import secrets
import string

ALPHABET = string.digits + "bcdfghjkmnpqrstvwxz"  # base36-like but without vowels to avoid accidental words

def generate_noid(length: int = 7) -> str:
    """
    Generate a random NOID-like string.
    
    Args:
        length: Length of the unique part (default 7)
        
    Returns:
        Random string using safe alphabet
    """
    return "".join(secrets.choice(ALPHABET) for _ in range(length))

def mint_ark_id(naan: str, shoulder: str = "") -> str:
    """
    Generate a full ARK identifier.
    
    Args:
        naan: Name Assigning Authority Number
        shoulder: Optional shoulder/prefix
        
    Returns:
        Full ARK string e.g. "ark:12345/xyz123" (new standard without slash after ark:)
    """
    suffix = generate_noid()
    if shoulder:
        return f"ark:{naan}/{shoulder}{suffix}"
    return f"ark:{naan}/{suffix}"

