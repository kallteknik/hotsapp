from typing import Any, Dict, Tuple, Optional

def _le_u16(buf: bytes, off: int) -> Optional[int]:
    return int.from_bytes(buf[off:off+2], "little") if off + 2 <= len(buf) else None

def _le_s16(buf: bytes, off: int) -> Optional[int]:
    if off + 2 > len(buf):
        return None
    v = int.from_bytes(buf[off:off+2], "little", signed=False)
    if v >= 0x8000:
        v -= 0x10000
    return v

def _md_payload(md: Any, company_id: int) -> Optional[bytes]:
    if not md:
        return None
    for k, v in md.items():
        try:
            if int(k) != company_id:
                continue
        except Exception:
            continue
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
        if isinstance(v, str):
            try:
                return bytes.fromhex(v)
            except Exception:
                return None
    return None

def decode(manufacturer_data: Any, address: str, prev_temp_c: Optional[float] = None) -> Optional[Dict[str, Any]]:
    payload = _md_payload(manufacturer_data, 16) or _md_payload(manufacturer_data, 17)
    if not payload or len(payload) < 6:
        return None

    mac_rev = b""
    if address:
        try:
            mac_rev = bytes.fromhex(address.replace(":", ""))[::-1]
        except Exception:
            pass

    candidates = []
    if mac_rev and mac_rev in payload:
        base = payload.index(mac_rev) + len(mac_rev)
        candidates += [base, base - 2, base + 2]
    candidates += [0, 2]

    best = None
    best_score = -1

    def parse_at(off: int):
        if off < 0 or off + 6 > len(payload):
            return -1, None
        batt_raw = _le_u16(payload, off)
        t_raw    = _le_s16(payload, off + 2)
        h_raw    = _le_u16(payload, off + 4)

        cand: Dict[str, Any] = {}
        score = 0

        temp_ok = False
        if t_raw is not None:
            temp_c = t_raw / 16.0
            if -40.0 <= temp_c <= 85.0:
                temp_ok = True
                temp_c = round(temp_c, 2)
                cand["temperature_c"] = temp_c
                score += 10
                if prev_temp_c is not None:
                    delta = abs(temp_c - float(prev_temp_c))
                    if   delta <= 0.5:  score += 5
                    elif delta <= 2.0:  score += 3
                    elif delta >= 8.0:  score -= 6

        if h_raw is not None:
            hum = h_raw / 16.0
            if 0.0 <= hum <= 100.0:
                cand["humidity_percent"] = round(hum, 2)
                score += 1

        if batt_raw is not None:
            batt_mv = batt_raw if batt_raw > 1000 else batt_raw * 10
            batt_v  = batt_mv / 1000.0
            if 2.0 <= batt_v <= 4.5:
                cand["battery_v"] = round(batt_v, 3)
                cand["battery_mv"] = int(batt_mv)
                score += 1

        return (score, cand) if temp_ok else (-1, None)

    seen = set()
    for off in candidates:
        if off in seen:
            continue
        seen.add(off)
        s, cand = parse_at(off)
        if s > best_score and cand:
            best_score = s
            best = cand

    return best
