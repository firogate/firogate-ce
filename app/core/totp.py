import pyotp
import qrcode
import io
import base64
import secrets
import hashlib
import hmac
import time


def generate_totp_secret():
    secret = pyotp.random_base32()
    return secret


def generate_totp_qr_code(username, secret, issuer="FiroGate"):
    secret_clean = str(secret).strip().upper()

    totp = pyotp.TOTP(secret_clean)
    uri = totp.provisioning_uri(name=username, issuer_name=issuer)

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=6,
        border=2,
    )
    qr.add_data(uri)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    img_base64 = base64.b64encode(buffer.getvalue()).decode()

    return f"data:image/png;base64,{img_base64}"


def verify_totp_code(secret, code, last_step=None):
    """Verify a 6-digit TOTP code.

    If `last_step` is given (the caller's previously-consumed time-step),
    reject any code from that step or earlier this blocks replay of an
    intercepted/leaked code within the +/-2-step (~90s) valid_window. Callers
    that pass `last_step` should persist `current_totp_step(...)` on success
    so the next verification can compare against it.
    """
    if not secret or not code:
        return False

    try:
        secret_clean = str(secret).strip().upper()
        code_clean = str(code).strip()

        if not secret_clean or not code_clean:
            return False

        if not code_clean.isdigit() or len(code_clean) != 6:
            return False

        totp = pyotp.TOTP(secret_clean)
        if not totp.verify(code_clean, valid_window=2):
            return False

        if last_step is not None:
            matched_step = _find_matching_step(totp, code_clean)
            if matched_step is not None and matched_step <= last_step:
                return False

        return True
    except Exception:
        return False


def current_totp_step(secret, code, valid_window=2):
    """Return the time-step counter that `code` matched against, or None."""
    try:
        secret_clean = str(secret).strip().upper()
        totp = pyotp.TOTP(secret_clean)
        return _find_matching_step(totp, str(code).strip(), valid_window=valid_window)
    except Exception:
        return None


def _find_matching_step(totp, code_clean, valid_window=2):
    now_step = int(time.time() / totp.interval)
    for offset in range(-valid_window, valid_window + 1):
        step = now_step + offset
        if hmac.compare_digest(totp.generate_otp(step), code_clean):
            return step
    return None


def generate_recovery_codes(count=10):
    codes = []
    for _ in range(count):
        code = base32_encode(secrets.token_bytes(5))[:8].upper()
        codes.append(code)
    return codes


def base32_encode(data):
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    result = ""
    bits = 0
    value = 0

    for byte in data:
        value = (value << 8) | byte
        bits += 8

        while bits >= 5:
            result += alphabet[(value >> (bits - 5)) & 31]
            bits -= 5

    if bits > 0:
        result += alphabet[(value << (5 - bits)) & 31]

    return result


def hash_recovery_code(code):
    return hashlib.sha256(code.encode()).hexdigest()


def encrypt_totp_secret(secret, fernet):
    secret_clean = str(secret).strip().upper()
    encrypted = fernet.encrypt(secret_clean.encode())
    return encrypted.decode()


def decrypt_totp_secret(encrypted_secret, fernet):
    try:
        encrypted_clean = str(encrypted_secret).strip()
        decrypted = fernet.decrypt(encrypted_clean.encode())
        decrypted_secret = decrypted.decode().strip().upper()

        if not decrypted_secret:
            return None

        import re
        if not re.match(r'^[A-Z2-7]+$', decrypted_secret):
            return None

        return decrypted_secret
    except Exception:
        return None


def init_totp_for_user(username, fernet=None):
    secret = generate_totp_secret()
    qr_code = generate_totp_qr_code(username, secret)

    recovery_codes = generate_recovery_codes()
    recovery_codes_hashed = [hash_recovery_code(code) for code in recovery_codes]

    result = {
        'secret': secret,
        'qr_code': qr_code,
        'recovery_codes': recovery_codes,
        'recovery_codes_hashed': recovery_codes_hashed
    }

    if fernet:
        result['secret_encrypted'] = encrypt_totp_secret(secret, fernet)
        decrypted_test = decrypt_totp_secret(result['secret_encrypted'], fernet)
        if decrypted_test != secret:
            raise ValueError("TOTP secret encryption/decryption validation failed")

    return result


