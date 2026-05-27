from app.core.config import get_settings


def calc_fee(amount: float) -> float:
    s = get_settings()
    fee = max(s.FEE_MIN_FIRO, round(amount * s.FEE_RATE_PCT / 100, 8))
    # Cap: fee never exceeds FEE_MAX_FIRO regardless of withdrawal size
    fee = min(fee, s.FEE_MAX_FIRO)
    return round(fee, 8)


def calc_net(amount: float) -> tuple[float, float]:
    fee = calc_fee(amount)
    net = round(amount - fee, 8)
    return fee, net
