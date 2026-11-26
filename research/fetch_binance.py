"""Κατεβάζει δημόσια ιστορικά δεδομένα Binance USDT-M futures (data.binance.vision) σε μικρά CSV.gz.

Έξοδος (στον φάκελο OUT):
  klines_<SYM>.csv.gz   ts,open,high,low,close,volume,taker_buy_volume   (ωριαία)
  funding_<SYM>.csv.gz  ts,rate                                          (ανά 8 ώρες)
  oi_<SYM>.csv.gz       ts,oi_usd                                        (ωριαία, από τα 5λεπτα metrics)
"""
import concurrent.futures as cf
import csv
import gzip
import io
import os
import sys
import time
import urllib.request
import zipfile
from datetime import date, timedelta

BASE = "https://data.binance.vision/data/futures/um"
SYMBOLS = os.environ.get("SYMS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,"
                                 "LTCUSDT,DOTUSDT,LUNAUSDT,FTTUSDT").split(",")
OI_SYMBOLS = os.environ.get("OI_SYMS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",")
START = date(2021, 1, 1)
OUT = sys.argv[1] if len(sys.argv) > 1 else "data"


def get(url):
    for i in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    return None


def unzip_rows(blob):
    if not blob:
        return []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        txt = z.read(z.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(txt)))
    return [r for r in rows if r and r[0][:1].isdigit()]   # χωρίς επικεφαλίδα


def months(until):
    d = START
    while d <= until:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def days(a, b):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def write(name, header, rows):
    with gzip.open(os.path.join(OUT, name), "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def klines(sym):
    today = date.today()
    last_full = date(today.year, today.month, 1) - timedelta(days=1)
    urls = [f"{BASE}/monthly/klines/{sym}/1h/{sym}-1h-{m:%Y-%m}.zip" for m in months(last_full)]
    urls += [f"{BASE}/daily/klines/{sym}/1h/{sym}-1h-{d:%Y-%m-%d}.zip"
             for d in days(date(today.year, today.month, 1), today - timedelta(days=1))]
    out = {}
    with cf.ThreadPoolExecutor(8) as ex:
        for rows in ex.map(lambda u: unzip_rows(get(u)), urls):
            for r in rows:
                ts = int(r[0])
                ts = ts // 1_000_000 if ts > 10**14 else ts // 1000
                out[ts] = [ts, r[1], r[2], r[3], r[4], r[5], r[9]]
    write(f"klines_{sym}.csv.gz", ["ts", "open", "high", "low", "close", "volume", "taker_buy_volume"],
          [out[k] for k in sorted(out)])
    return len(out)


def funding(sym):
    today = date.today()
    out = {}
    with cf.ThreadPoolExecutor(8) as ex:
        urls = [f"{BASE}/monthly/fundingRate/{sym}/{sym}-fundingRate-{m:%Y-%m}.zip" for m in months(today)]
        for rows in ex.map(lambda u: unzip_rows(get(u)), urls):
            for r in rows:
                out[int(r[0]) // 1000] = r[2]
    write(f"funding_{sym}.csv.gz", ["ts", "rate"], [[k, out[k]] for k in sorted(out)])
    return len(out)


def oi(sym):
    today = date.today()
    out = {}
    urls = [f"{BASE}/daily/metrics/{sym}/{sym}-metrics-{d:%Y-%m-%d}.zip"
            for d in days(date(2021, 12, 1), today - timedelta(days=1))]
    with cf.ThreadPoolExecutor(16) as ex:
        for rows in ex.map(lambda u: unzip_rows(get(u)) if True else [], urls):
            for r in rows:
                # create_time,symbol,sum_open_interest,sum_open_interest_value,...
                try:
                    from datetime import datetime
                    ts = int(datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S").timestamp())
                except Exception:
                    continue
                if ts % 3600 == 0:
                    out[ts] = r[3]
    write(f"oi_{sym}.csv.gz", ["ts", "oi_usd"], [[k, out[k]] for k in sorted(out)])
    return len(out)


def main():
    os.makedirs(OUT, exist_ok=True)
    os.environ["TZ"] = "UTC"
    time.tzset()
    for s in SYMBOLS:
        print(s, "klines", klines(s), "funding", funding(s), flush=True)
    for s in OI_SYMBOLS:
        print(s, "oi", oi(s), flush=True)


if __name__ == "__main__":
    main()
