from cryptography.fernet import Fernet

from db_guardian.crypto import SecretBox


def test_roundtrip_and_ciphertext_differs():
    box = SecretBox(Fernet.generate_key().decode())
    plain = 'p@ss "#\\ ='
    token = box.encrypt(plain)
    assert token != plain
    assert box.decrypt(token) == plain
