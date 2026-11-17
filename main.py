# BTC Race 24h — btc-race-24h-20261117-0603
# Kandidat D: "dip 0,50% awal + exit pump 3% + fallback all-in" —
# REVISI SPESIFIKASI USER atas kandidat C (15:17 WIB 17 Nov 2026).
# Turunan C dengan DUA angka diganti (DIP_BPS 200->50, EXIT_BPS
# 30->300); seluruh struktur dan aturan lain IDENTIK dengan C.
# Ditulis fresh di sesi builder ini.
# ENTRI FINAL instance ini per keputusan user 16:48 WIB 17 Nov 2026:
# kandidat D yang di-submit; v4 (probe-first, HEAD d927498) tetap
# terarsip di histori git repo ini sebagai pembanding, tidak di-submit.
#
# Aturan (tanpa tambahan apa pun di luar spec):
#   1. Referensi tetap = close bar pertama jendela (bar 0, harga
#      pembuka race). Kaki dip aktif sejak bar 0.
#   2. Kaki dip: pertama kali close <= referensi x (1 - 0,50%),
#      BELI ALL-IN (100% ekuitas). Tepat SATU siklus.
#   3. Exit kaki dip: JUAL SEMUA saat close >= harga masuk rata-rata
#      x (1 + 3,00%) — "sell saat pump 3%", relatif terhadap harga
#      beli. Bila tidak pernah tercapai sampai akhir: tahan sampai
#      flatten paksa harness di close terakhir. TANPA stop loss,
#      TANPA kunci lain.
#   4. Fallback: dinilai SATU KALI di bar 1410 — bila saat itu bot
#      TIDAK memegang posisi (dip tidak pernah datang, ATAU siklus
#      dip sudah selesai terjual sebelum bar 1410), BELI ALL-IN di
#      pasar dan tahan sampai akhir. Bila di bar 1410 posisi dip
#      masih terbuka, fallback tidak berlaku sama sekali.
#   5. Tidak ada kaki lain. Tidak ada probe. Tidak ada momentum.
# Buy yang ditolak harness dicoba lagi selama kondisinya masih
# berlaku, supaya "beli all-in" benar-benar terjadi — tetap tepat
# satu posisi per kaki. Status kaki dicatat saat order terkirim
# (pola yang sama dengan entri v4); penolakan terdeteksi dari
# posisi yang tidak muncul dalam 2 batch, lalu kaki bersenjata lagi.
#
# Deterministik: keputusan murni fungsi bar yang terlihat. Tanpa
# jam, tanpa environment, tanpa randomness, stdlib saja.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
SCALE = 100_000_000
DIP_BPS = 50            # dip 0,50% di bawah referensi bar 0 (revisi user 15:17 WIB)
EXIT_BPS = 300          # jual semua di +3,00% di atas harga masuk rata-rata ("sell saat pump 3%")
FALLBACK_BAR = 1410     # awal 30 menit terakhir
SLIP_BPS = 300          # longgar supaya fill selamat saat open loncat
MIN_ORDER = 1_000_000   # 1 USDC, minimum hello

_ref = None             # close bar 0, dipatok sekali
_dip_state = 0          # 0 = bersenjata, 1 = order terkirim/posisi terbuka, 2 = siklus selesai
_dip_order_seq = -1     # bar saat buy dip terkirim
_exit_sent = False      # sell exit +3,00% sudah terkirim
_fallback_open = False  # buy fallback sudah terkirim / posisi fallback terbuka
_fallback_over = False  # penilaian fallback di bar 1410 sudah lewat
_fallback_buy = False   # penilaian bar 1410 memutuskan membeli (bot flat)
_fb_order_seq = -1      # bar saat buy fallback terkirim


def on_hello(state):
    global _ref, _dip_state, _dip_order_seq, _exit_sent
    global _fallback_open, _fallback_over, _fallback_buy, _fb_order_seq
    _ref = None
    _dip_state = 0
    _dip_order_seq = -1
    _exit_sent = False
    _fallback_open = False
    _fallback_over = False
    _fallback_buy = False
    _fb_order_seq = -1


def _all_in(state):
    size = state.equity
    room = state.weight_room(SYMBOL)
    if size > room:
        size = room
    if size > state.cash:
        size = state.cash
    if size < MIN_ORDER:
        return []
    return [{"side": "buy", "symbol": SYMBOL,
             "notional_quote_units": int(size),
             "max_slippage_bps": SLIP_BPS}]


def on_batch(state, batch):
    global _ref, _dip_state, _dip_order_seq, _exit_sent
    global _fallback_open, _fallback_over, _fallback_buy, _fb_order_seq

    bar = state.latest.get(SYMBOL)
    if bar is None:
        return []
    if state.warming_up:
        return []
    seq = state.seq
    close = int(bar["c"])
    held = state.positions.get(SYMBOL)

    # Patok referensi di bar pertama yang terlihat.
    if _ref is None:
        _ref = close

    # --- rekonsiliasi kaki dip dengan posisi nyata ---
    if _dip_state == 1 and held is None:
        if _exit_sent:
            _dip_state = 2          # exit terisi: siklus selesai
        elif seq >= _dip_order_seq + 2:
            _dip_state = 0          # buy ditolak: bersenjata lagi

    # --- rekonsiliasi fallback: buy ditolak -> boleh dicoba lagi ---
    if _fallback_open and held is None and seq >= _fb_order_seq + 2 \
            and _dip_state != 1:
        _fallback_open = False

    # Penilaian fallback gugur bila di bar 1410 posisi masih terbuka:
    # bar 1410 adalah satu-satunya titik penilaian.
    if seq >= FALLBACK_BAR and held is not None:
        _fallback_over = True

    # ---- posisi kaki dip terbuka: hanya exit +3,00% yang berlaku ----
    if held is not None and _dip_state == 1:
        # close >= entry_avg x (1 + EXIT_BPS/10000) persis setara
        # dengan nilai posisi >= cost (termasuk fee) x faktor yang
        # sama — perbandingan bebas satuan, memakai value/cost
        # posisi dari SDK.
        value = state.value(SYMBOL, held.qty)
        if held.cost > 0 and value * 10_000 >= held.cost * (10_000 + EXIT_BPS):
            if value >= MIN_ORDER:
                _exit_sent = True
                return [{"side": "sell", "symbol": SYMBOL,
                         "qty": int(held.qty),
                         "max_slippage_bps": SLIP_BPS}]
        return []

    # ---- posisi fallback terbuka: tahan sampai akhir, tanpa exit ----
    if held is not None:
        return []

    # ---- flat: kaki dip (satu siklus, aktif sejak bar 0) ----
    if _dip_state == 0 and close * 10_000 <= _ref * (10_000 - DIP_BPS):
        orders = _all_in(state)
        if orders:
            _dip_state = 1
            _dip_order_seq = seq
            _exit_sent = False
        return orders

    # ---- flat: penilaian fallback satu kali di bar 1410 ----
    if seq >= FALLBACK_BAR and not _fallback_over:
        _fallback_over = True
        _fallback_buy = True  # sampai di sini berarti bot flat
        orders = _all_in(state)
        if orders:
            _fallback_open = True
            _fb_order_seq = seq
        return orders

    # Buy fallback ditolak di bar 1410: coba lagi selama masih flat.
    if _fallback_buy and not _fallback_open:
        orders = _all_in(state)
        if orders:
            _fallback_open = True
            _fb_order_seq = seq
        return orders

    return []


makefun_bars.run(on_batch, on_hello=on_hello)
