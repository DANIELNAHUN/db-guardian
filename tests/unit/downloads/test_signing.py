from db_guardian.downloads.signing import sign, verify

KEY = "k" * 32


def test_valid_signature_verifies():
    sig = sign(7, 1000, KEY)
    assert verify(7, 1000, sig, KEY, now=999)
    assert verify(7, 1000, sig, KEY, now=1000)


def test_expired_signature_rejected():
    assert not verify(7, 1000, sign(7, 1000, KEY), KEY, now=1001)


def test_tampered_values_rejected():
    sig = sign(7, 1000, KEY)
    assert not verify(8, 1000, sig, KEY, now=0)
    assert not verify(7, 2000, sig, KEY, now=0)
    assert not verify(7, 1000, sig[:-1] + ("0" if sig[-1] != "0" else "1"), KEY, now=0)
    assert not verify(7, 1000, sig, "z" * 32, now=0)
    assert not verify(7, 1000, "not-hex", KEY, now=0)
