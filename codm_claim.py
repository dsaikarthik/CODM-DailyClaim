
import argparse
import json
import sys
import uuid
import urllib.request
import urllib.parse
import urllib.error

UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")

LEAP = "https://shopapi.codashop.com"
WHITELABEL_ID = 1          
VOUCHER_TYPE = "CALL_OF_DUTY_MOBILE_WL"
DAILY_REFRESH_RATE = 86400   

RESULT_CODES = {
    0: "SUCCESS",
    1201: "ALREADY_CLAIMED",
    1202: "VALIDITY_EXPIRED",
    1203: "NOT_ELIGIBLE",
    1210: "PUBLISHER_SERVICE_ERROR",
    3009: "INVALID_REGION",
}


def http_json(url, payload, headers=None):
    h = {"Content-Type": "application/json", "User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "ignore"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"_network_error": str(e)}


def leap_headers(country, locale="en-in"):
    return {
        "Accept-Language": locale,
        "X-EXPT-TOKEN": "",
        "X-EXPT-CONTEXT": "",
        "X-WHITELABEL-ID": str(WHITELABEL_ID),
        "X-SESSION-COUNTRY2NAME": country.upper(),
    }


FALLBACK_REGIONS = ["sa", "sg", "us", "eu", "me", "la", "af"]


def _validate_once(player_id, country, region):
    url = f"https://order-{region}.codashop.com/validate"
    payload = {
        "country": country.upper(),
        "voucherTypeName": VOUCHER_TYPE,
        "whiteLabelId": str(WHITELABEL_ID),
        "deviceId": str(uuid.uuid4()),
        "userId": player_id,
        "zoneId": "",
    }
    return http_json(url, payload, headers={"Accept-Language": ""})


def validate(player_id, country, region, _seen=None):

    if _seen is None:
        _seen = set()
    regions = FALLBACK_REGIONS if region == "auto" else [region]
    last_err = None
    for r in regions:
        key = (r, country)
        if key in _seen:
            continue
        _seen.add(key)
        st, data = _validate_once(player_id, country, r)
        if st != 200 or data.get("_network_error"):
            last_err = f"validate via {r}: HTTP {st} {json.dumps(data)[:200]}"
            continue
        if data.get("errorCode") == -200:            # wrong ckuntry
            home = data.get("homeBaseCountry2Name")
            if home:
                res, err, meta = validate(player_id, home, region, _seen)
                if res:
                    return res, err, meta
                last_err = err
                continue
            last_err = f"validate via {r}: -200 but no homeBaseCountry2Name"
            continue
        if data.get("success") is False:
            return None, f"validate failed: {data.get('errorMsg') or data.get('errorCode')}", None
        result = data.get("result") or {}
        profile = {
            "username": result.get("username") or result.get("nickname"),
            "shortId": result.get("shortId"),
            "picUrl": result.get("picUrl"),
            "levelImage": result.get("customLevelImageUrl"),
            "rank": result.get("customReadableMpRank"),
            "rankImage": result.get("customMpRankImageUrl"),
        }
        return profile, None, {"region": r, "country": country}
    return None, last_err or "validate: no region responded", None


def product_page(country, product_path, locale):
    errs = []
    for loc in [locale, f"ar-{country.lower()}", "en-in"]:
        st, data = http_json(
            LEAP + "/productPage",
            {"productPath": product_path, "locale": loc, "whitelabelId": WHITELABEL_ID},
            headers=leap_headers(country, loc),
        )
        if st == 200 and data.get("productInfo"):
            break
        errs.append(f"{loc}: HTTP {st} {json.dumps(data)[:200]}")
    else:
        return None, f"productPage {product_path}: no product for country {country} ({'; '.join(errs)})"
    pi = data.get("productInfo") or {}
    pc = None
    channels = data.get("paymentChannels") or []
    if channels:
        pc = channels[0].get("id")
    if pc is None:
        for sku in data.get("skus") or []:
            prices = ((sku.get("pricing") or {}).get("paymentChannelPrices") or {})
            if prices:
                pc = next(iter(prices))
                break
    return {
        "productUrl": pi.get("productUrl", product_path),
        "lvtId": pi.get("id"),
        "gvtId": pi.get("gvtId"),
        "voucherTypeId": pi.get("voucherTypeId"),
        "voucherTypeName": pi.get("voucherTypeName"),
        "paymentChannelId": pc or 391,
    }, None


def dynamic_sku_info(country, device_id, user_id, prod, locale):
    st, data = http_json(
        LEAP + "/productPage/dynamicSkuInfo",
        {
            "deviceId": device_id,
            "whitelabelId": WHITELABEL_ID,
            "userId": user_id,
            "serverId": "",
            "characterId": "",
            "worldId": "",
            "locale": locale,
            "productPath": prod["productUrl"],
        },
        headers=leap_headers(country, locale),
    )
    if st != 200:
        return None, None, f"dynamicSkuInfo HTTP {st}: {json.dumps(data)[:300]}"
    skus = data.get("skus") or []
    freebies = []
    for s in skus:
        scheme = ((s.get("pricing") or {}).get("pricingScheme") or "").upper()
        if scheme != "FREEBIE" or "claim" not in (s.get("BuyUrl") or ""):
            continue
        lim = s.get("PurchaseLimit") or {}
        freebies.append({
            "skuId": s.get("Id"),
            "name": s.get("SkuName"),
            "status": s.get("Status"),
            "buyUrl": s.get("BuyUrl"),
            "remaining": lim.get("limitRemaining"),
            "limit": lim.get("limit"),
            "refreshRate": lim.get("refreshRate"),
            "refreshAtUnix": lim.get("refreshAtUnix"),
        })
    return data, freebies, None


def create_order_token(country, prod, sku, page_lock_token, locale="en-in"):
    st, data = http_json(
        LEAP + "/productPage/createOrderToken",
        {
            "pageLockToken": page_lock_token,
            "productPath": prod["productUrl"],
            "skuId": sku["skuId"],
            "paymentChannelId": prod["paymentChannelId"],
            "whitelabelId": WHITELABEL_ID,
        },
        headers=leap_headers(country, locale),
    )
    if st != 200:
        return None, f"createOrderToken HTTP {st}: {json.dumps(data)[:300]}"
    token = data.get("dynamicSkuToken")
    if not token:
        return None, f"createOrderToken: no dynamicSkuToken in {json.dumps(data)[:200]}"
    return token, None


def build_claim_form(prod, sku, user_id, token, status, shop_lang):
    return {
        "shopLang": shop_lang,
        "user.userId": user_id,
        "user.zoneId": "",
        "checkoutId": str(uuid.uuid4()),         
        "dynamicSkuToken": token,
        "status": status,                          # "A" = ACTIVE , it is what it is..
        "lvtId": str(prod["lvtId"]),
        "skuId": sku["skuId"],
        "pricingScheme": "freebie",
        "gvtId": str(prod["gvtId"]),
        "voucherTypeId": str(prod["voucherTypeId"]),
        "voucherTypeName": prod["voucherTypeName"],
        "callOrderAPI": "false",
    }


def claim(sku, form):
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        sku["buyUrl"], data=body, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://store.callofdutymobile.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
    except Exception as e:
        return 0, {"_network_error": str(e)}
    try:
        return 200, json.loads(raw)
    except Exception:
        return 200, {"_raw": raw[:500]}


def pick_freebie(freebies, claim_all):
    avail = [f for f in freebies if f["status"] == "ACTIVE" and f["remaining"]]
    avail.sort(key=lambda f: (f["refreshRate"] != DAILY_REFRESH_RATE, f["remaining"] == 0))
    if claim_all:
        return avail
    return avail[:1]


def main():
    ap = argparse.ArgumentParser(description="Claim CODM store DAILY GIFT (secret cache)")
    ap.add_argument("player_id", help="your in-game Player ID or short ID")
    ap.add_argument("--country", default="IN",
                    help="initial country guess for the lookup (default IN; the API auto-corrects it and the "
                         "claim always uses the account's real country)")
    ap.add_argument("--region", default="auto", help="codashop region (default auto; lookup works on any region)")
    ap.add_argument("--dry-run", action="store_true", help="verify + print, do not POST claim")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--status", default=None, help="claim status param (default: first char of SKU status; repo-era value: 1)")
    ap.add_argument("--all", action="store_true", help="claim every available freebie, not just the daily gift")
    args = ap.parse_args()

    out = {"player_id": args.player_id, "region": args.region,
           "country_guess": args.country.upper()}
    exit_code = 0

    profile, err, meta = validate(args.player_id, args.country, args.region)
    if err:
        out.update({"ok": False, "step": "validate", "error": err})
        print(json.dumps(out, indent=2) if args.json else err)
        return 3
    out["profile"] = profile
    out["resolved"] = meta
    claim_user = profile["shortId"] or args.player_id

    resolved_country = (meta or {}).get("country") or args.country.upper()
    out["country"] = resolved_country   # actual result used in final request 
    cc = resolved_country.lower()
    product_path = f"/{cc}/codm"
    locale = f"en-{cc}"
    shop_lang = f"en_{cc}"

  
    prod, err = product_page(resolved_country, product_path, locale)
    if err:
        out.update({"ok": False, "step": "productPage", "error": err})
        print(json.dumps(out, indent=2) if args.json else err)
        return 3

   
    device_id = str(uuid.uuid4())
    sku_user = profile["shortId"] or args.player_id.upper()  
    data, freebies, err = dynamic_sku_info(resolved_country, device_id, sku_user, prod, locale)
    if err:
        out.update({"ok": False, "step": "dynamicSkuInfo", "error": err})
        print(json.dumps(out, indent=2) if args.json else err)
        return 3

    targets = pick_freebie(freebies, args.all)
    out["freebies"] = freebies
    if not targets:
        msg = "no claimable freebie: "
        if not freebies:
            msg += "no FREEBIE skus returned by the store"
        else:
            states = ", ".join(f"{f['name']} ({f['status']}, remaining {f['remaining']})" for f in freebies)
            msg += states
        out.update({"ok": False, "step": "select", "error": msg})
        print(json.dumps(out, indent=2) if args.json else msg)
        return 2

   
    results = []
    for sku in targets:
        token, err = create_order_token(resolved_country, prod, sku, data.get("pageLockToken") or "", locale)
        if err:
            results.append({"skuId": sku["skuId"], "ok": False, "step": "createOrderToken", "error": err})
            exit_code = exit_code or 3
            continue
        form = build_claim_form(prod, sku, claim_user, token,
                                args.status or (sku["status"] or "ACTIVE")[0], shop_lang)
        if args.dry_run:
            results.append({
                "skuId": sku["skuId"], "name": sku["name"], "ok": True,
                "dry_run": True, "claim_url": sku["buyUrl"], "form": form,
                "next_refresh_unix": sku["refreshAtUnix"],
            })
            continue
        _, resp = claim(sku, form)
        code = resp.get("RESULT_CODE")
        code_name = RESULT_CODES.get(code, f"UNKNOWN({code})")
        results.append({
            "skuId": sku["skuId"], "name": sku["name"], "ok": code == 0,
            "result_code": code, "result": code_name,
            "response": {k: v for k, v in resp.items() if k in ("RESULT_CODE", "errorMsg", "errorCode", "message", "orderId")},
        })
        if code == 0:
            continue
        if code == 1201:    
            exit_code = exit_code or 0
        else:
            exit_code = 3

    out.update({"ok": all(r.get("ok", False) for r in results), "claims": results})
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for r in results:
            if r.get("dry_run"):
                print(f"[dry-run] would claim {r['name']} ({r['skuId']}) -> {r['claim_url']}")
                print("          form:", json.dumps(r["form"]))
            else:
                print(f"{r['name']}: {r['result']}" + (f" ({r['response']})" if r["response"] else ""))
        if profile:
            print(f"player: {profile.get('username') or '?'}" + (f" [{profile['shortId']}]" if profile.get("shortId") else ""))
        if results and results[0].get("next_refresh_unix"):
            import datetime
            print("next daily refresh (UTC):",
                  datetime.datetime.fromtimestamp(results[0]["next_refresh_unix"], datetime.timezone.utc).isoformat())
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
