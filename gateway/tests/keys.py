"""A stand-in for the phone's hardware key: a P-256 key that signs as Android's does."""
import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


class PhoneKey:
    def __init__(self):
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.public = base64.b64encode(self.private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()

    def sign(self, message):
        return base64.b64encode(self.private.sign(message.encode(), ec.ECDSA(hashes.SHA256()))).decode()


KEY = PhoneKey()
